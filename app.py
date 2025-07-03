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
    data = profile_col.find_one({"user_id": user_id})
    if not data:
        # 預設值
        return {
            "energy_level": 70,
            "physical_level": 70,
            "income_this_month": 0,
            "expense_this_month": 0,
            "safe_line": 20000,
            "money_alert": False,
            "time_core": "核心時段：6-10 睡眠, 10-18 工作, 18-24 彈性",
            "students": [],
            "teaching_logs": [],
            "transaction_history": []
        }
    return data

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
        return False, ""
    data = profile_col.find_one({"user_id": user_id})
    income = data.get("income_this_month", 0)
    expense = data.get("expense_this_month", 0)
    safe_line = data.get("safe_line", 20000)
    diff = income - expense
    alert = diff < safe_line
    profile_col.update_one({"user_id": user_id}, {"$set": {"money_alert": alert}})
    msg = f"⚡️ 金流警戒！目前差額 {diff} 已低於安全線！" if alert else ""
    return alert, msg

def check_teaching_log_reminder(user_id):
    tz = pytz.timezone('Asia/Taipei')
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    logs = profile_col.find_one({"user_id": user_id}).get("teaching_logs", [])
    for log in logs:
        if log.get("date") == str(yesterday):
            return ""
    return f"⚠️ 提醒：昨天（{yesterday}）未登錄教學紀錄，需補上？"

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
    user_state = get_user_state(user_id)

    try:
        # 狀態更新
        if "狀態：" in user_message:
            match = re.match(r"狀態：(\d{1,3})/(\d{1,3})", user_message)
            if match:
                update_user_state(user_id, int(match.group(1)), int(match.group(2)))
                line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已更新狀態：精神{match.group(1)} 體力{match.group(2)}"))
                return

        # 收入紀錄
        if "收到" in user_message and re.search(r"\d+元", user_message):
            amount = int(re.search(r"(\d+)元", user_message).group(1))
            profile_col.update_one({"user_id": user_id}, {"$inc": {"income_this_month": amount}})
            profile_col.update_one({"user_id": user_id}, {"$push": {"transaction_history": {"type": "income", "amount": amount, "time": str(now)}}})
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"💰 已紀錄收入 +{amount} 元"))
            return

        # 學生請假
        if "請假" in user_message:
            found = [s['name'] for s in user_state.get('students', []) if s['name'] in user_message]
            if found:
                total_unpaid = len(found) * 500  # 或動態計算
                profile_col.update_one({"user_id": user_id}, {"$inc": {"unpaid_this_month": total_unpaid}})
                line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已紀錄請假：{'、'.join(found)}（未收款已更新）"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("⚠️ 沒找到符合的學生名稱"))
            return

        # 教學紀錄
        teaching_match = re.search(r"(.*?)教了(\d+)小時", user_message)
        if teaching_match:
            name = teaching_match.group(1).strip()
            hours = int(teaching_match.group(2))
            tz = pytz.timezone('Asia/Taipei')
            today_date = datetime.now(tz).date()
            week_str = f"{today_date.isocalendar().year}-W{today_date.isocalendar().week}"
            profile_col.update_one({"user_id": user_id}, {"$push": {"teaching_logs": {"name": name, "hours": hours, "date": str(today_date), "week": week_str}}})
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已紀錄：{name} 上課 {hours} 小時 (週次: {week_str})"))
            return

    except Exception as e:
        traceback.print_exc()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"⚠️ 執行時發生錯誤：{e}"))
        return

    if mongo_ok:
        col.insert_one({"user_id": user_id, "role": "user", "content": user_message, "time": now})

    money_alert, money_message = check_money_alert(user_id)
    structure_summary = f"""
⏰【時間骨架】{user_state.get('time_core')}
🧠【精神力】{user_state.get('energy_level', 70)}/100
💪【體力】{user_state.get('physical_level', 70)}/100
💰【金流】安全線 {user_state.get('safe_line', 20000)} → {'⚠️ 警戒' if money_alert else '✅ 正常'}
"""
    daily = f"📅 今日摘要：{len(list(col.find({'user_id': user_id})))} 筆對話"
    teaching_reminder = check_teaching_log_reminder(user_id)

    system_prompt = f"""
你是小老虎AI，專屬於蘇有維，真實、溫暖、結構化陪跑。
📍 時間：{now_str} {period}
🌦️ 天氣：{weather_str}
{structure_summary}
{daily}
{teaching_reminder}{money_message}
請結合用戶訊息，回應具體安排並帶一句暖心提醒。
"""

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

    segments = []
    buf = ""
    for line in ai_reply.split('\n'):
        if len(buf) + len(line) < 1000:
            buf += line + '\n'
        else:
            segments.append(buf.strip())
            buf = line
    if buf:
        segments.append(buf.strip())
    segments = segments[:5]

    if mongo_ok:
        col.insert_one({"user_id": user_id, "role": "assistant", "content": ai_reply, "time": now})
    try:
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=s) for s in segments])
    except Exception as e:
        print("❌ LINE 回覆失敗", e)

# ---------- 6. 健康檢查 ----------
@app.route('/health', methods=['GET'])
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
