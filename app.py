import os
from dotenv import load_dotenv
from datetime import datetime
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from pymongo import MongoClient
import openai
import requests
import re
import traceback

# ---------- 1. 讀取環境變數 ----------
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
CWA_API_KEY = os.getenv("CWA_API_KEY")

# ---------- 2. 初始化 ----------
app = Flask(__name__)
try:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client['gptdb']
    col = db['chats']
    profile_col = db['profiles']
    mongo_ok = True
except Exception as e:
    print("❌ 初始化失敗：", e)
    mongo_ok = False

# ---------- 3. 工具 ----------
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
        return "（尚未設定天氣API）"
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={CWA_API_KEY}&locationName=臺北市"
        res = requests.get(url, timeout=5)
        data = res.json()
        el = data['records']['location'][0]['weatherElement']
        wx_now = el[0]['time'][0]['parameter']['parameterName']
        pop_now = el[1]['time'][0]['parameter']['parameterName']
        minT_now = el[2]['time'][0]['parameter']['parameterName']
        maxT_now = el[4]['time'][0]['parameter']['parameterName']
        return f"台北現在：{wx_now}（降雨{pop_now}%），氣溫 {minT_now}~{maxT_now}°C"
    except Exception as e:
        return f"（天氣查詢失敗：{e}）"

def auto_split_lines(text, max_line_len=70):
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

# ---------- 4. 狀態管理 ----------
def get_user_state(user_id):
    if not mongo_ok:
        return {}
    latest = profile_col.find_one({"user_id": user_id})
    if latest:
        return {
            "energy": latest.get("energy_level", 70),
            "physical": latest.get("physical_level", 70),
            "money_alert": latest.get("money_alert", False),
            "must_do": latest.get("must_do", ["倒垃圾", "核對金流", "洗澡"]),
            "time_core": latest.get("time_core", "核心時段：6-10 睡眠, 10-18 工作, 18-24 彈性"),
            "money_safe_line": latest.get("safe_line", 20000),
            "students": latest.get("students", [])
        }
    return {
        "energy": 70, "physical": 70, "money_alert": False,
        "must_do": ["倒垃圾", "核對金流", "洗澡"],
        "time_core": "核心時段：6-10 睡眠, 10-18 工作, 18-24 彈性",
        "money_safe_line": 20000,
        "students": []
    }

def update_user_state(user_id, energy, physical):
    if not mongo_ok:
        return
    profile_col.update_one(
        {"user_id": user_id},
        {"$set": {"energy_level": energy, "physical_level": physical}},
        upsert=True
    )

def check_money_alert(user_id):
    if not mongo_ok:
        return False
    data = profile_col.find_one({"user_id": user_id})
    income = data.get("income_this_month", 0)
    expense = data.get("expense_this_month", 0)
    safe_line = data.get("safe_line", 20000)
    alert = (income - expense) < safe_line
    profile_col.update_one({"user_id": user_id}, {"$set": {"money_alert": alert}})
    return alert

def get_daily_summary(user_id):
    if not mongo_ok:
        return "（無法生成今日摘要）"
    tz = pytz.timezone('Asia/Taipei')
    today = datetime.now(tz).date()
    start = tz.localize(datetime.combine(today, datetime.min.time()))
    msgs = list(col.find({"user_id": user_id, "time": {"$gte": start}}))
    modules_done = [m['content'] for m in msgs if m['role'] == 'assistant']
    return f"📅 今日摘要：已完成 {len(modules_done)} 條互動，請回顧必做底線是否執行！"

def get_monthly_summary(user_id):
    if not mongo_ok:
        return "（無法生成月摘要）"
    tz = pytz.timezone('Asia/Taipei')
    today = datetime.now(tz)
    first_day = tz.localize(today.replace(day=1))
    msgs = list(col.find({"user_id": user_id, "time": {"$gte": first_day}}))
    return f"📅 本月摘要：累計對話 {len(msgs)} 條，請留意現金流安全線！"

# ---------- 5. Webhook ----------
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
    weather_str = get_taipei_weather()

    if "狀態：" in user_message:
        match = re.match(r"狀態：(\d{1,3})/(\d{1,3})", user_message)
        if match:
            update_user_state(user_id, int(match.group(1)), int(match.group(2)))
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已更新狀態：精神{match.group(1)} 體力{match.group(2)}"))
            return

    if "收到" in user_message and re.search(r"\d+元", user_message):
        amount = int(re.search(r"(\d+)元", user_message).group(1))
        profile_col.update_one({"user_id": user_id}, {"$inc": {"income_this_month": amount}})
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"💰 已紀錄收入 +{amount} 元"))
        return

    if "請假" in user_message:
        user_state = get_user_state(user_id)
        students = user_state.get('students', [])
        found = [s['name'] for s in students if s['name'] in user_message]
        if found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已紀錄請假：{'、'.join(found)}"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⚠️ 沒找到符合的學生名稱！"))
        return

    if mongo_ok:
        col.insert_one({"user_id": user_id, "role": "user", "content": user_message, "time": now})

    user_state = get_user_state(user_id)
    check_money_alert(user_id)

    modules = []
    if user_state['energy'] > 70:
        modules += ["高專注備課", "重大決策", "創意策劃"]
    elif user_state['energy'] > 40:
        modules += ["學生聯繫", "環境整理"]
    else:
        modules += ["金流檢查", "放鬆儀式"]
    modules += user_state["must_do"]
    modules = list(set(modules))

    structure_summary = (
        f"⏰【時間骨架】{user_state['time_core']}\n"
        f"🧠【精神力】{user_state['energy']}/100\n"
        f"💪【體力】{user_state['physical']}/100\n"
        f"💰【金流】安全線 {user_state['money_safe_line']} → {'⚠️ 警戒' if user_state['money_alert'] else '✅ 正常'}"
    )
    daily = get_daily_summary(user_id)
    monthly = get_monthly_summary(user_id)

    system_prompt = (
        f"你是小老虎AI，專屬於蘇有維，真實、溫暖、彈性結構化陪跑。\n"
        f"📍 時間：{now_str} {period}\n"
        f"🌦️ 天氣：{weather_str}\n"
        f"{structure_summary}\n"
        f"📌 今日推薦模組：{modules}\n"
        f"{daily}\n{monthly}\n"
        "請結合用戶訊息與狀態，給予實用安排，並帶一句暖心提醒！"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        traceback.print_exc()
        ai_reply = f"AI錯誤：{e}"

    ai_reply = auto_split_lines(ai_reply)
    if mongo_ok:
        col.insert_one({"user_id": user_id, "role": "assistant", "content": ai_reply, "time": now})

    MAX_LEN = 1000
    segments = [ai_reply[i:i+MAX_LEN] for i in range(0, len(ai_reply), MAX_LEN)]
    line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=seg) for seg in segments])

# ---------- 6. 健康檢查 ----------
@app.route('/health', methods=['GET'])
def health():
    return 'ok', 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
