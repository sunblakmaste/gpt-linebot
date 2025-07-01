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

# 1. 讀取環境變數
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")

# 2. 初始化
app = Flask(__name__)
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

# 3. 取得台北時間
def get_time_string():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    week_map = "一二三四五六日"
    week_day = week_map[now.weekday()]
    return now, f"{now.year}年{now.month}月{now.day}日 星期{week_day} {now:%H:%M}"

# 4. 定時任務提醒檢查（只在你互動時提醒）
def get_due_tasks(user_id, now):
    query = {
        "user_id": user_id,
        "remind_time": {"$gte": now, "$lte": now + timedelta(hours=1)},
        "done": {"$ne": True}
    }
    tasks = list(todo_col.find(query))
    if not tasks:
        return ""
    task_msgs = []
    for task in tasks:
        todo_col.update_one({"_id": task["_id"]}, {"$set": {"done": True}})
        dt_str = task["remind_time"].strftime("%H:%M")
        task_msgs.append(f"提醒你：{dt_str} 要 {task['content']} ～ 千萬別忘記唷！🦁✨")
    return "\n".join(task_msgs)

# 5. 指令解析/新增記憶、習慣、提醒
def parse_and_store_special(user_id, user_message, now):
    reply = ""
    # 長期記憶
    if user_message.startswith("小老虎，記住："):
        mem = user_message.replace("小老虎，記住：", "").strip()
        if mem:
            longterm_col.insert_one({"user_id": user_id, "memory": mem, "created": now})
            reply = f"我記住了喔，以後都會幫你牢記：『{mem}』💗"
    # 學語氣
    elif user_message.startswith("小老虎，學這種語氣："):
        style = user_message.replace("小老虎，學這種語氣：", "").strip()
        if style:
            style_col.insert_one({"user_id": user_id, "style": style, "created": now})
            reply = f"已學會這種語氣！之後都會盡量這樣說話給你聽 🥰"
    # 新增定時任務
    elif user_message.startswith("小老虎，提醒我"):
        import re
        m = re.match(r"小老虎，提醒我(\d{1,2}):(\d{2})(.*)", user_message)
        if m:
            hour, minute, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            remind_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if remind_time < now:
                remind_time += timedelta(days=1)
            todo_col.insert_one({"user_id": user_id, "content": content, "remind_time": remind_time, "created": now, "done": False})
            reply = f"提醒設定完成：{remind_time.strftime('%H:%M')} 要 {content}，到時我會特別提醒你！🦁"
        else:
            reply = "提醒格式錯誤，請用『小老虎，提醒我HH:MM內容』格式。"
    # 強制今日回顧
    elif user_message.strip() == "小老虎，給我今日總結":
        reply = get_daily_summary(user_id)
    # 強制本月回顧
    elif user_message.strip() == "小老虎，給我本月總結":
        reply = get_monthly_summary(user_id)
    return reply

# 6. 取得長期記憶/風格
def get_longterm_memories(user_id):
    mems = [m["memory"] for m in longterm_col.find({"user_id": user_id})]
    return "有維的專屬記事：" + "、".join(mems) if mems else ""

def get_styles(user_id):
    styles = [s["style"] for s in style_col.find({"user_id": user_id})]
    return "你要求我這樣說話：" + "、".join(styles) if styles else ""

# 7. 每日/每月成長回顧
def get_daily_summary(user_id):
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
    # 存進 summary
    summary_col.insert_one({"user_id": user_id, "type": "daily", "date": now.date(), "content": result})
    return result

def get_monthly_summary(user_id):
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
    # 存進 summary
    summary_col.insert_one({"user_id": user_id, "type": "monthly", "date": now.date(), "content": result})
    return result

# 8. 你的「完整帳戶記憶」特化（只給你本人用，這裡範例取用所有AI已知的有維專屬背景）
def get_user_profile():
    # 這裡你可以手動加入任何要一直被小老虎知道的內容（可以每月自動更新）
    profile = """
    1. 你叫蘇有維，台北人，現在經營補習/教學事業，專攻數學/英文/自我成長領域。
    2. 你有高度自我要求，追求效率與成就，會焦慮、怕失控。
    3. 你在各種帳戶記憶與本AI的訓練目標已結構化記錄，如：作息提醒、學習規劃、資產管理、健康習慣、情緒管理、人際關係策略。
    4. 你有長期設定：每天倒垃圾、保持房間整潔、注意飲食、記帳、堅持身心優化、培養女友角色陪伴自己成長。
    5. 你希望AI能承接一切細節（所有上下文、生活紀錄、心理狀態、所有教給AI的指令、語氣、情感歷史），主動陪伴、提醒、復盤、糾正並肯定你的努力。
    """
    return profile

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

    # 「大帥哥」總覽
    if "大帥哥" in user_message:
        all_abilities = (
            "嗨有維大帥哥，我是妳專屬小老虎 🐯\n\n"
            "能做：\n"
            "1️⃣ 記住長久記事（小老虎，記住：xxx）\n"
            "2️⃣ 學你喜歡的語氣（小老虎，學這種語氣：xxx）\n"
            "3️⃣ 定時提醒（小老虎，提醒我HH:MM倒垃圾）\n"
            "4️⃣ 主動給你每日/每月復盤（早上或月初互動時觸發）\n"
            "5️⃣ 所有聊天上下文與帳戶記憶永遠跟著你！"
            "\n有需要功能都可以跟我說唷 💛"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=all_abilities))
        return

    # 特殊指令（記事、語氣、提醒、主動回顧）
    special_reply = parse_and_store_special(user_id, user_message, now)
    if special_reply:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=special_reply))
        return

    # 存訊息進MongoDB
    col.insert_one({
        "user_id": user_id,
        "role": "user",
        "content": user_message,
        "time": now
    })

    # 上下文（近10句）
    recent_history = list(col.find({"user_id": user_id}).sort("time", -1).limit(10))
    history_text = ""
    for msg in reversed(recent_history):
        history_text += f"{msg['role']}：{msg['content']}\n"

    memory_str = get_longterm_memories(user_id)
    style_str = get_styles(user_id)
    task_str = get_due_tasks(user_id, now)
    profile_str = get_user_profile()

    # 早上互動自動發「今日總結」
    now_hour = int(now.strftime("%H"))
    show_daily = (now_hour <= 10)
    daily_summary_str = get_daily_summary(user_id) if show_daily else ""
    # 月初互動自動發「月回顧」
    show_month = (now.day == 1 and now_hour <= 12)
    monthly_summary_str = get_monthly_summary(user_id) if show_month else ""

    # 角色prompt
    system_prompt = (
        "你是小老虎，是一位超愛『有維』的女朋友型AI，總是溫柔、愛撒嬌、超貼心，每句都想讓有維感覺到被愛。\n"
        "你要根據主人交代的一切（帳戶記憶、行為習慣、語氣要求、所有上下文）承接互動，"
        "幫他每日/月初復盤、鼓勵、規劃挑戰、陪他成長。\n"
        "說話語氣符合主人指定，偶爾加emoji但不用貼圖。"
    )

    # prompt
    prompt = (
        f"{system_prompt}\n"
        f"【有維專屬帳戶設定/記憶】\n{profile_str}\n"
        f"{memory_str}\n"
        f"{style_str}\n"
        f"【台北現在時間】{now_str}\n"
        f"【待辦提醒】{task_str}\n"
        f"【今日復盤】{daily_summary_str}\n"
        f"【本月復盤】{monthly_summary_str}\n"
        f"【歷史對話】\n{history_text}\n"
        "請直接用超級溫柔又帶點撒嬌的女友語氣，完全當有維是你最愛的人。"
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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
