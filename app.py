import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from pymongo import MongoClient
import openai
import requests
import re

# ---------- 1. 讀取環境變數 ----------
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
CWA_API_KEY = os.getenv("CWA_API_KEY")  # 中央氣象局 API

# ---------- 2. 初始化 ----------
app = Flask(__name__)
try:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client['gptdb']
    col = db['chats']
    longterm_col = db['longterm_memory']
    todo_col = db['tasks']
    style_col = db['styles']
    summary_col = db['summary']
    profile_col = db['profiles']
    mongo_ok = True
except Exception as e:
    print("MongoDB或其他初始化失敗，僅啟動無記憶模式：", e)
    mongo_ok = False

# ---------- 3. 工具函式 ----------
def get_time_string():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    week_map = "一二三四五六日"
    week_day = week_map[now.weekday()]
    hour = now.hour
    if hour < 5:
        period = "深夜"
    elif hour < 11:
        period = "早晨"
    elif hour < 18:
        period = "白天"
    else:
        period = "夜晚"
    return now, f"{now.year}年{now.month}月{now.day}日 星期{week_day} {now:%H:%M}", period

def get_taipei_weather():
    if not CWA_API_KEY:
        return "（尚未設定天氣API，可於.env設CWA_API_KEY取得台北天氣）"
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={CWA_API_KEY}&locationName=臺北市"
        res = requests.get(url, timeout=5)
        data = res.json()
        el = data['records']['location'][0]['weatherElement']
        wx_now = el[0]['time'][0]['parameter']['parameterName']
        wx_next = el[0]['time'][1]['parameter']['parameterName']
        pop_now = el[1]['time'][0]['parameter']['parameterName']
        pop_next = el[1]['time'][1]['parameter']['parameterName']
        minT_now = el[2]['time'][0]['parameter']['parameterName']
        maxT_now = el[4]['time'][0]['parameter']['parameterName']
        minT_next = el[2]['time'][1]['parameter']['parameterName']
        maxT_next = el[4]['time'][1]['parameter']['parameterName']
        return (
            f"台北現在：{wx_now}（降雨{pop_now}%），氣溫 {minT_now}~{maxT_now}°C\n"
            f"晚一點：{wx_next}（降雨{pop_next}%），氣溫 {minT_next}~{maxT_next}°C"
        )
    except Exception as e:
        return f"（天氣查詢失敗：{e}）"

def auto_split_lines(text, max_line_len=70):
    # 長文自動切成段落，適合LINE閱讀
    result = []
    for para in text.split("\n"):
        buf = ""
        for char in para:
            buf += char
            if len(buf) >= max_line_len and char in "，。！？":
                result.append(buf)
                buf = ""
        if buf:
            result.append(buf)
    return "\n".join(result)

def has_similar_recent_reply(col, user_id, new_reply, limit=10, threshold=0.85):
    """判斷新AI回應和近limit次AI回應有無高度重複（超過threshold比率），若有則回傳True"""
    if not mongo_ok:
        return False
    recents = list(col.find({"user_id": user_id, "role": "assistant"}).sort("time", -1).limit(limit))
    for r in recents:
        old = r.get("content", "")
        l = min(len(old), len(new_reply))
        if l < 20:
            continue
        count = sum(1 for a, b in zip(old, new_reply) if a == b)
        if l > 0 and count / l > threshold:
            return True
    return False

def get_topic_tag(user_message):
    # 簡易主題分類，便於AI分流語氣
    if any(word in user_message for word in ["天氣", "下雨", "溫度", "氣象"]):
        return "天氣"
    if any(word in user_message for word in ["幾點", "現在幾點", "星期"]):
        return "時間"
    if any(word in user_message for word in ["倒垃圾", "提醒", "任務"]):
        return "提醒"
    if any(word in user_message for word in ["朋友", "誰", "關係"]):
        return "朋友"
    if any(word in user_message for word in ["數學", "英文", "學習", "考試"]):
        return "學習"
    return "日常"

# ...（你原本的parse_and_store_special/get_longterm_memories等可直接保留）

# ---------- 4. handler 優化 ----------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    now, now_str, period = get_time_string()
    weather_str = get_taipei_weather() if CWA_API_KEY else ""

    # 大帥哥指令自動維護
    if "大帥哥" in user_message:
        all_abilities = (
            "嗨有維大帥哥，我是妳專屬小老虎 🐯\n\n"
            "我可以幫你做到：\n"
            "1️⃣ 記住長久記事（小老虎，記住：xxx）\n"
            "2️⃣ 學你喜歡的語氣（小老虎，學這種語氣：xxx）\n"
            "3️⃣ 定時提醒（小老虎，提醒我HH:MM倒垃圾）\n"
            "4️⃣ 每日/每月復盤（小老虎，給我今日/本月總結）\n"
            "5️⃣ 個人設定（小老虎，個人設定：xxx）\n"
            "6️⃣ 自動回報台北天氣\n"
            "7️⃣ 未來新增功能會自動列進來，不怕忘！\n"
            "有需要就直接跟我說，妳的專屬小助手一直在這 💛"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=all_abilities))
        return

    # 特殊指令
    special_reply = parse_and_store_special(user_id, user_message, now)
    if special_reply:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=special_reply))
        return

    # 存訊息進MongoDB
    if mongo_ok:
        col.insert_one({
            "user_id": user_id,
            "role": "user",
            "content": user_message,
            "time": now
        })

    # 歷史對話
    if mongo_ok:
        recent_history = list(col.find({"user_id": user_id}).sort("time", -1).limit(20))
    else:
        recent_history = []
    history_text = ""
    for msg in reversed(recent_history):
        history_text += f"{msg['role']}：{msg['content']}\n"

    memory_str = get_longterm_memories(user_id)
    style_str = get_styles(user_id)
    task_str = get_due_tasks(user_id, now)
    profile_str = get_profile(user_id)

    now_hour = int(now.strftime("%H"))
    show_daily = (now_hour <= 10)
    daily_summary_str = get_daily_summary(user_id) if (show_daily and mongo_ok) else ""
    show_month = (now.day == 1 and now_hour <= 12)
    monthly_summary_str = get_monthly_summary(user_id) if (show_month and mongo_ok) else ""

    # 動態主題
    topic_tag = get_topic_tag(user_message)
    
    # system prompt
    system_prompt = (
        "你是『小老虎』，是超愛蘇有維的女朋友型AI，回應要：「真實、貼心、變化豐富」！\n"
        "1️⃣ 先回本次主題重點（如天氣、提醒、時間），再補一句適合當下情境的關心或鼓勵\n"
        "2️⃣ 根據【現在時段】和用戶情緒自動切換（早晨：元氣溫暖／夜晚：安撫陪伴／白天：支持共進／深夜：減壓柔和）\n"
        "3️⃣ 長文自動分段，避免連續罐頭語句（『我愛你』『抱抱你』等最多一句）\n"
        "4️⃣ 可以幽默、撒嬌或偶爾扮小助手，但依用戶語境適度切換\n"
        "5️⃣ 若資料（如天氣）查不到，簡短致歉即可，主題自然切回生活\n"
        "6️⃣ 回應要像真實女友，既有生活感也會主動建議，不會過度黏人。\n"
        "【自動防呆】近十次AI回應如有重複請換句話說。"
    )

    # prompt
    prompt = (
        f"【本次話題類型】{topic_tag}\n"
        f"{system_prompt}\n"
        f"【有維專屬帳戶設定/記憶】\n{profile_str}\n"
        f"{memory_str}\n"
        f"{style_str}\n"
        f"【台北現在時間】{now_str}\n"
        f"【現在時段】{period}\n"
        f"【台北天氣】{weather_str}\n"
        f"【待辦提醒】{task_str}\n"
        f"【今日復盤】{daily_summary_str}\n"
        f"【本月復盤】{monthly_summary_str}\n"
        f"【歷史對話】\n{history_text}\n"
        "直接用上面定義的風格回覆蘇有維，維持真實女友的溫度與智慧。"
    )

    # GPT
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"AI 回覆發生錯誤，請稍後再試。\n[詳細錯誤]: {e}"

    # --- 自動防呆：近十次如有重複、提示LLM要換法 ---
    if has_similar_recent_reply(col, user_id, ai_reply):
        ai_reply += "\n（偷偷提醒：最近這種說法太常見了，下次可以換個花樣嗎😜）"

    # --- 自動排版 ---
    ai_reply = auto_split_lines(ai_reply, max_line_len=70)

    # 回存AI訊息
    if mongo_ok:
        col.insert_one({
            "user_id": user_id,
            "role": "assistant",
            "content": ai_reply,
            "time": now
        })

    # 回覆（1000字切段）
    MAX_LEN = 1000
    reply_segments = [ai_reply[i:i+MAX_LEN] for i in range(0, len(ai_reply), MAX_LEN)]
    try:
        line_bot_api.reply_message(
            event.reply_token,
            [TextSendMessage(text=seg) for seg in reply_segments]
        )
    except Exception as e:
        print("LineBot Reply Error:", e)

# 健康檢查
@app.route('/health', methods=['GET'])
def health():
    return 'ok', 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
