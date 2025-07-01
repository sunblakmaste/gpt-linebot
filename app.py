import os
from dotenv import load_dotenv

# 載入 .env
load_dotenv()
print("TOKEN:", os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))  # debug用，部署前可移除

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from pymongo import MongoClient
import openai
from datetime import datetime

# 金鑰初始化
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")

# 初始化物件
app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# openai v1.x 新語法
client = openai.OpenAI(api_key=OPENAI_API_KEY)

try:
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client['gptdb']
    col = db['chats']
except Exception as e:
    print("MongoDB 連線失敗:", e)
    col = None

# LINE Webhook
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    timestamp = datetime.now()
    print(f"[{timestamp}] {user_id}：{user_message}")

    # 存進 MongoDB
    try:
        if col:
            col.insert_one({
                "user_id": user_id,
                "role": "user",
                "content": user_message,
                "time": timestamp
            })
    except Exception as e:
        print("MongoDB 存取錯誤：", e)

    # 查詢歷史
    try:
        if col:
            recent_history = list(col.find({"user_id": user_id}).sort("time", -1).limit(10))
            context = ""
            for msg in reversed(recent_history):
                context += f"{msg['role']}: {msg['content']}\n"
        else:
            context = f"user: {user_message}\n"
    except Exception as e:
        context = f"user: {user_message}\n"
        print("MongoDB 查詢錯誤：", e)

    # 丟 GPT
    prompt = "你是用戶的個人AI助理，請根據下列真實歷史對話回應，不要幻想或亂編：\n" + context
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"AI 回覆發生錯誤，請稍後再試。\n\n[詳細錯誤]: {e}"
        print("OpenAI Error:", e)

    # 切段（LINE單則上限 1000字，避免超過）
    MAX_LEN = 1000
    reply_segments = [ai_reply[i:i+MAX_LEN] for i in range(0, len(ai_reply), MAX_LEN)]

    # AI 回覆存進 MongoDB
    try:
        if col:
            col.insert_one({
                "user_id": user_id,
                "role": "assistant",
                "content": ai_reply,
                "time": datetime.now()
            })
    except Exception as e:
        print("MongoDB 存取錯誤（回覆階段）：", e)

    # 回傳訊息
    try:
        line_bot_api.reply_message(
            event.reply_token,
            [TextSendMessage(text=seg) for seg in reply_segments]
        )
    except Exception as e:
        print("LineBot Reply Error:", e)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)


