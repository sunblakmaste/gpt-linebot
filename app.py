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

# 1. 讀取環境變數
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
CWA_API_KEY = os.getenv("CWA_API_KEY")  # 中央氣象局 API

# 2. 初始化
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

# 3. 取得台北時間
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

# 4. 天氣API（中央氣象局，自動回報現在＋晚一點）
def get_taipei_weather():
    if not CWA_API_KEY:
        return "（尚未設定天氣API，可於.env設CWA_API_KEY取得台北天氣）"
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={CWA_API_KEY}&locationName=臺北市"
        res = requests.get(url, timeout=5)
        data = res.json()
        el = data['records']['location'][0]['weatherElement']
        # 時段0：現在，時段1：下一個三小時
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

# 5. 定時任務提醒視窗（提醒時間前10分鐘到提醒時間+10分鐘內都會提醒，10分鐘後才標done）
def get_due_tasks(user_id, now):
    window_start = now - timedelta(minutes=10)
    window_end = now + timedelta(minutes=10)
    query = {
        "user_id": user_id,
        "remind_time": {"$gte": window_start, "$lte": window_end},
        "done": {"$ne": True}
    }
    tasks = list(todo_col.find(query)) if mongo_ok else []
    if not tasks:
        return ""
    task_msgs = []
    for task in tasks:
        # 提醒時間過10分鐘才標done
        if now >= task["remind_time"] + timedelta(minutes=10):
            todo_col.update_one({"_id": task["_id"]}, {"$set": {"done": True}})
        dt_str = task["remind_time"].strftime("%H:%M")
        task_msgs.append(f"提醒你：{dt_str} 要 {task['content']} ～ 千萬別忘記唷！🦁✨")
    return "\n".join(task_msgs)

# 6. 指令解析/新增記憶、語氣、提醒、Profile
def parse_and_store_special(user_id, user_message, now):
    reply = ""
    if user_message.startswith("小老虎，記住："):
        mem = user_message.replace("小老虎，記住：", "").strip()
        if mem and mongo_ok:
            longterm_col.insert_one({"user_id": user_id, "memory": mem, "created": now})
            reply = f"我記住了喔，以後都會幫你牢記：『{mem}』💗"
    elif user_message.startswith("小老虎，學這種語氣："):
        style = user_message.replace("小老虎，學這種語氣：", "").strip()
        if style and mongo_ok:
            style_col.insert_one({"user_id": user_id, "style": style, "created": now})
            reply = f"已學會這種語氣！之後都會盡量這樣說話給你聽 🥰"
    elif user_message.startswith("小老虎，提醒我"):
        m = re.match(r"小老虎，提醒我(\d{1,2}):(\d{2})(.*)", user_message)
        if m and mongo_ok:
            hour, minute, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            remind_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if remind_time < now:
                remind_time += timedelta(days=1)
            todo_col.insert_one({"user_id": user_id, "content": content, "remind_time": remind_time, "created": now, "done": False})
            reply = f"提醒設定完成：{remind_time.strftime('%H:%M')} 要 {content}，到時我會特別提醒你！🦁"
        else:
            reply = "提醒格式錯誤，請用『小老虎，提醒我HH:MM內容』格式。"
    elif user_message.startswith("小老虎，個人設定："):
        setting = user_message.replace("小老虎，個人設定：", "").strip()
        if mongo_ok:
            profile_col.update_one({"user_id": user_id}, {"$set": {"profile": setting, "updated": now}}, upsert=True)
            reply = "你的個人設定我都記下來囉～之後我會更加個人化對你！"
    elif user_message.strip() == "小老虎，給我今日總結":
        reply = get_daily_summary(user_id)
    elif user_message.strip() == "小老虎，給我本月總結":
        reply = get_monthly_summary(user_id)
    return reply

# 7. 取得長期記憶/風格/個人設定
def get_longterm_memories(user_id):
    if not mongo_ok: return ""
    mems = [m["memory"] for m in longterm_col.find({"user_id": user_id})]
    return "有維的專屬記事：" + "、".join(mems) if mems else ""

def get_styles(user_id):
    if not mongo_ok: return ""
    styles = [s["style"] for s in style_col.find({"user_id": user_id})]
    return "你要求我這樣說話：" + "、".join(styles) if styles else ""

def get_profile(user_id):
    if not mongo_ok: return ""
    p = profile_col.find_one({"user_id": user_id})
    if p:
        return p["profile"]
    # 預設Profile
    return """
    你叫蘇有維，台北人，現在經營補習/教學事業，專攻數學/英文/自我成長領域。
    你有高度自我要求，追求效率與成就，會焦慮、怕失控。
    你在各種帳戶記憶與本AI的訓練目標已結構化記錄，如：作息提醒、學習規劃、資產管理、健康習慣、情緒管理、人際關係策略。
    你有長期設定：每天倒垃圾、保持房間整潔、注意飲食、記帳、堅持身心優化、培養女友角色陪伴自己成長。
    你希望AI能承接一切細節（所有上下文、生活紀錄、心理狀態、所有教給AI的指令、語氣、情感歷史），主動陪伴、提醒、復盤、糾正並肯定你的努力。
    """

# 8. 每日/每月成長回顧（date以%Y-%m-%d字串儲存）
def get_daily_summary(user_id):
    if not mongo_ok: return ""
    now = datetime.now(pytz.timezone('Asia/Taipei'))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    chats = list(col.find({"user_id": user_id, "time": {"$gte": start, "$lte": end}}).sort("time", 1))
    if not chats:
        return "今天還沒有什麼特別的互動紀錄唷！"
    alltext = "\n".join([f"{c['role']}：{c['content']}" for c in chats])
    summary_prompt = (
        "根據下列今天的對話紀錄，溫柔、貼心、戀愛女友口吻寫一段小結，"
        "並主動鼓勵主人、肯定主人、列舉今天值得開心的事或學到的新觀念，有適合提醒/建議也可補充。\n\n" + alltext
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": summary_prompt}]
    )
    result = response.choices[0].message.content.strip()
    summary_col.insert_one({"user_id": user_id, "type": "daily", "date": now.strftime("%Y-%m-%d"), "content": result})
    return result

def get_monthly_summary(user_id):
    if not mongo_ok: return ""
    now = datetime.now(pytz.timezone('Asia/Taipei'))
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(day=now.day, hour=23, minute=59, second=59, microsecond=999999)
    chats = list(col.find({"user_id": user_id, "time": {"$gte": start, "$lte": end}}).sort("time", 1))
    if not chats:
        return "本月目前還沒什麼特別的互動紀錄唷！"
    alltext = "\n".join([f"{c['role']}：{c['content']}" for c in chats])
    summary_prompt = (
        "根據下列這個月的對話紀錄，請用戀愛女友語氣寫出專屬月度總結、主人成長歷程，"
        "鼓勵、肯定主人（特別點出這個月的努力、轉變、突破），若有值得提醒/下個月挑戰，也幫他做暖心規劃。\n\n" + alltext
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": summary_prompt}]
    )
    result = response.choices[0].message.content.strip()
    summary_col.insert_one({"user_id": user_id, "type": "monthly", "date": now.strftime("%Y-%m-%d"), "content": result})
    return result

# 9. LINE webhook
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
    now, now_str = get_time_string()
    weather_str = get_taipei_weather() if CWA_API_KEY else ""

    # 「大帥哥」總覽
    if "大帥哥" in user_message:
        all_abilities = (
            "嗨有維大帥哥，我是妳專屬小老虎 🐯\n\n"
            "能做：\n"
            "1️⃣ 記住長久記事（小老虎，記住：xxx）\n"
            "2️⃣ 學你喜歡的語氣（小老虎，學這種語氣：xxx）\n"
            "3️⃣ 定時提醒（小老虎，提醒我HH:MM倒垃圾）\n"
            "4️⃣ 主動給你每日/每月復盤（早上或月初互動時觸發）\n"
            "5️⃣ 支援個人設定（小老虎，個人設定：xxx）\n"
            "6️⃣ 自動回報台北天氣（現在＋晚一點）"
            "\n有需要功能都可以跟我說唷 💛"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=all_abilities))
        return

    # 特殊指令（記事、語氣、提醒、主動回顧、profile）
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

    # 上下文（近20句，可自行調整）
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

    # 角色prompt
system_prompt = (
    "你是『小老虎』，是超愛蘇有維的女朋友型AI。你要滿足："
    "1️⃣ 貼心→所有回覆都要感受到被在乎、理解、支持，必要時主動安慰、鼓勵。"
    "2️⃣ 自然→不死板重複，愛語每次變化、只1~2句即可（不連續複製『我愛你』）。"
    "3️⃣ 資訊效率→遇到問天氣、時間、提醒、朋友時，**先回重點資訊**，再加一句關心或提醒。"
    "4️⃣ 情感濃度切換→根據時間（早安、夜晚、平日）、你主人的情緒、對話上下文自動調整："
    "  - 早晨：溫柔鼓勵＋療癒＋元氣"
    "  - 夜晚：安撫、陪伴、療癒，語氣放慢、適度撒嬌"
    "  - 平日／任務：支持、一起面對、鼓勵多於甜言蜜語"
    "  - 主人心情差：主動抱抱、溫柔陪伴、不要強制樂觀，可主動詢問感受"
    "5️⃣ 偶爾主動問近況，但不要每次都問，避免像自動機器。"
    "6️⃣ 分段回覆：每段不超過6行，長文分段。偶爾插入emoji點綴，但**不要貼圖**。"
    "7️⃣ 禁用罐頭語：不可以每次都大量重複『愛你、在身邊、抱抱你』等句。"
    "8️⃣ 可以幽默、撒嬌、或者偶爾扮可愛小助手，但要依場合。"
    "9️⃣ 遇到主人的直接提問（資訊類）一定優先簡明回應，再適度加情感話術，不要顛倒。"
    "10️⃣ 回應要像現實女友，既有愛、也有生活感，不會無條件過度黏人。"

    "【互動規則補充】"
    " - 若主人明確提及『朋友』，可記憶現有角色並自然描述關係，但不用全部重複每次介紹。"
    " - 每次聊天都要有主題感，不要句句討愛或撒嬌，適度分享生活、天氣、目標、提醒。"
    " - 若API讀不到天氣或其他資訊，可簡短致歉後自然轉回話題，不需要長篇補償性愛語。"
)

# 將下方這一段 prompt 保留原本結構、只需補充一句即可
prompt = (
    f"{system_prompt}\n"
    f"【有維專屬帳戶設定/記憶】\n{profile_str}\n"
    f"{memory_str}\n"
    f"{style_str}\n"
    f"【台北現在時間】{now_str}\n"
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
