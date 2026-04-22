import logging
import requests
import json
import re
from datetime import datetime
from collections import defaultdict

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    PollAnswerHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ─────────────────────────────────────────
# ЖӨНДӨМӨЛӨР — ушуларды өзгөрт
# ─────────────────────────────────────────
TELEGRAM_TOKEN = "8432060922:AAFpdpoGqmlCt-kmaw_rvgoLELy7RQbHLzQ"
DIFY_API_KEY   = "app-HELigDeYtAHAl13XadJTmbAx"
DIFY_API_URL   = "https://api.dify.ai/v1/chat-messages"

TIMEZONE         = pytz.timezone("Asia/Bishkek")
TEST_DAYS        = {2: "Шейшемби", 4: "Бейшемби", 7: "Жекшемби"}
REMINDER_HOUR    = 9
REMINDER_MINUTE  = 0
WEEKLY_STAT_DAY  = 7
WEEKLY_STAT_HOUR = 20
QUESTIONS_PER_TEST = 15

# ─────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ─────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# МААЛЫМАТ САКТАГЫЧ
# ─────────────────────────────────────────
# user_data[uid] = {
#   "conversation_id": str,
#   "current_topic": str,
#   "questions": [ {question, options, correct, explanation} ],
#   "current_q": int,           # учурдагы суроо индекси
#   "correct_count": int,
#   "wrong_indexes": [int],     # туура эмес жооп берген суроолор
#   "poll_map": {poll_id: q_index},
#   "sessions": [ {topic, correct, total, date} ],
# }
user_data: dict = defaultdict(lambda: {
    "conversation_id": "",
    "current_topic": "",
    "questions": [],
    "current_q": 0,
    "correct_count": 0,
    "wrong_indexes": [],
    "poll_map": {},
    "sessions": [],
})
registered_users: set = set()

# ─────────────────────────────────────────
# ТЕМАЛАР
# ─────────────────────────────────────────
TOPICS = {
    "python":     "Python",
    "postgresql": "PostgreSQL",
    "oop":        "OOP",
    "fastapi":    "FastAPI",
    "django":     "Django REST",
    "streamlit":  "Streamlit",
    "pytorch":    "PyTorch",
    "docker":     "Docker",
    "aws":        "AWS",
    "ml":         "Machine Learning",
    "dl":         "Deep Learning",
    "pandas":     "Pandas",
    "numpy":      "NumPy",
    "seaborn":    "Seaborn",
    "matplotlib": "Matplotlib",
}

# ─────────────────────────────────────────
# DIFY — JSON суроо алуу
# ─────────────────────────────────────────
DIFY_SYSTEM_SUFFIX = """
Суроону так мындай JSON форматта гана кайтар, башка эч нерсе жазба,
markdown блок да колдонба, жалан таза JSON:
{
  "question": "Суроонун тексти",
  "options": ["Биринчи вариант", "Экинчи вариант", "Үчүнчү вариант", "Төртүнчү вариант"],
  "correct": 0,
  "explanation": "Кыска себеп (1-2 сүйлөм)"
}
"correct" — туура жооптун индекси (0,1,2 же 3).
"""

def ask_dify_json(uid: int, prompt: str, conv_id: str = "") -> dict | None:
    """Dify'ден JSON суроо алат. 3 жолу кайра баштайт."""
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(3):
        try:
            payload = {
                "inputs": {},
                "query": prompt + DIFY_SYSTEM_SUFFIX,
                "response_mode": "blocking",
                "conversation_id": conv_id,
                "user": str(uid),
            }
            res = requests.post(DIFY_API_URL, json=payload, headers=headers, timeout=30)

            if res.status_code == 429:
                # Rate limit — 3 секунд күт
                import time
                time.sleep(3)
                continue

            data = res.json()
            new_conv = data.get("conversation_id", "")
            if new_conv:
                conv_id = new_conv

            raw = data.get("answer", "")
            # JSON блогун тазала
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
            # Кээде жооптун алдында текст болот — JSON'ду гана алабыз
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                raw = match.group(0)

            parsed = json.loads(raw)
            # Валидация
            assert "question" in parsed
            assert isinstance(parsed["options"], list) and len(parsed["options"]) == 4
            assert isinstance(parsed["correct"], int) and 0 <= parsed["correct"] <= 3
            parsed["_conv_id"] = conv_id
            return parsed

        except Exception as e:
            logger.error(f"ask_dify_json attempt {attempt+1} error: {e}")
            import time
            time.sleep(2)

    return None

def ask_dify_text(uid: int, message: str) -> str:
    """Кадимки текст жооп алат."""
    conv_id = user_data[uid]["conversation_id"]
    payload = {
        "inputs": {},
        "query": message,
        "response_mode": "blocking",
        "conversation_id": conv_id,
        "user": str(uid),
    }
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        res = requests.post(DIFY_API_URL, json=payload, headers=headers, timeout=30)
        data = res.json()
        new_conv = data.get("conversation_id", "")
        if new_conv:
            user_data[uid]["conversation_id"] = new_conv
        return data.get("answer", "Жооп алуу мүмкүн болгон жок.")
    except Exception as e:
        logger.error(f"ask_dify_text error: {e}")
        return "Туташуу катасы. Кайра баштап көр."

# ─────────────────────────────────────────
# ТЕСТ ЛОГИКАСЫ
# ─────────────────────────────────────────
async def load_questions(uid: int, topic: str, is_retry: bool = False) -> bool:
    """
    Тест үчүн суроолорду Dify'ден жүктөйт.
    is_retry=True болсо — мурунку туура эмес суроолорду кайра жүктөйт.
    """
    ud = user_data[uid]

    if is_retry:
        wrong_idxs = ud["wrong_indexes"]
        if not wrong_idxs:
            return False
        # Мурунку туура эмес суроолорду кайра колдон
        retry_qs = [ud["questions"][i] for i in wrong_idxs]
        ud["questions"]    = retry_qs
        ud["current_q"]    = 0
        ud["correct_count"] = 0
        ud["wrong_indexes"] = []
        ud["poll_map"]      = {}
        return True

    # Жаңы суроолор жүктө — жаңы conversation баштайбыз
    ud["questions"]     = []
    ud["current_q"]     = 0
    ud["correct_count"] = 0
    ud["wrong_indexes"] = []
    ud["poll_map"]      = {}
    ud["current_topic"] = topic
    ud["test_conv_id"]  = ""

    prompt = (
        f"{topic} темасынан 1 суроо бер. "
        f"Суроо жаңы болсун. Бул 1-суроо."
    )
    q = ask_dify_json(uid, prompt, conv_id="")
    if q:
        ud["test_conv_id"] = q.pop("_conv_id", "")
        ud["questions"].append(q)
        return True
    return False

async def send_next_poll(update_or_chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Кийинки суроону Poll катары жибер."""
    ud  = user_data[uid]
    idx = ud["current_q"]
    qs  = ud["questions"]

    if idx >= len(qs):
        # Суроо жок — жаңысын жүктө
        topic = ud["current_topic"]
        total = len(qs)
        q_num = total + 1

        if total >= QUESTIONS_PER_TEST:
            await finish_test(update_or_chat, uid, ctx)
            return

        prompt = (
            f"{topic} темасынан 1 суроо бер. "
            f"Мурунку суроолорду кайталаба. "
            f"Бул {q_num}-суроо."
        )
        conv_id = ud.get("test_conv_id", "")
        q = ask_dify_json(uid, prompt, conv_id=conv_id)
        if not q:
            await _send(update_or_chat, ctx, uid,
                        "Суроо жүктөөдө ката болду. /reset деп кайра баштап көр.")
            return
        ud["test_conv_id"] = q.pop("_conv_id", conv_id)
        qs.append(q)

    q = qs[idx]
    total = QUESTIONS_PER_TEST
    topic = ud["current_topic"]

    # Poll жибер
    chat_id = uid
    msg = await ctx.bot.send_poll(
        chat_id=chat_id,
        question=f"[{idx+1}/{total}] {topic}\n\n{q['question']}",
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct"],
        explanation=q.get("explanation", ""),
        is_anonymous=False,
        open_period=60,
    )
    ud["poll_map"][msg.poll.id] = idx

async def finish_test(update_or_chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Тест аяктагандан кийин жыйынтык чыгар."""
    ud      = user_data[uid]
    correct = ud["correct_count"]
    total   = len(ud["questions"])
    topic   = ud["current_topic"]
    pct     = round(correct / total * 100) if total else 0

    # Статистикага жаз
    ud["sessions"].append({
        "topic":   topic,
        "correct": correct,
        "total":   total,
        "date":    datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
    })

    emoji = "🏆" if pct >= 80 else ("👍" if pct >= 50 else "📚")
    text  = (
        f"{emoji} *Тест аяктады!*\n\n"
        f"Тема: {topic}\n"
        f"Натыйжа: *{correct}/{total}* ({pct}%)\n"
    )

    wrong_count = len(ud["wrong_indexes"])
    if wrong_count:
        text += f"Туура эмес: {wrong_count} суроо\n\n"
        text += "Туура эмес суроолорду кайра иштегиң келсе /retry деп жаз."
    else:
        text += "\nБардык суроолор туура! 💯"

    await _send(update_or_chat, ctx, uid, text)

async def _send(update_or_chat, ctx, uid: int, text: str):
    """Жөнөкөй текст жибер."""
    try:
        if hasattr(update_or_chat, "message") and update_or_chat.message:
            await update_or_chat.message.reply_text(text, parse_mode="Markdown")
        else:
            await ctx.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"_send error: {e}")

# ─────────────────────────────────────────
# POLL ЖООП HANDLER
# ─────────────────────────────────────────
async def handle_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Колдонуучу Poll'го жооп берсе иштейт."""
    answer  = update.poll_answer
    uid     = answer.user.id
    poll_id = answer.poll_id
    ud      = user_data[uid]

    q_idx = ud["poll_map"].get(poll_id)
    if q_idx is None:
        return

    selected = answer.option_ids[0] if answer.option_ids else -1
    correct  = ud["questions"][q_idx]["correct"]

    if selected == correct:
        ud["correct_count"] += 1
    else:
        ud["wrong_indexes"].append(q_idx)

    ud["current_q"] = q_idx + 1

    # Тест бүттүбү?
    if ud["current_q"] >= QUESTIONS_PER_TEST:
        await finish_test(None, uid, ctx)
    else:
        # Кийинки суроону жибер
        class FakeUpdate:
            pass
        fake = FakeUpdate()
        await send_next_poll(fake, uid, ctx)

# ─────────────────────────────────────────
# FUNCTION ТАПШЫРМАЛАРЫ
# ─────────────────────────────────────────
async def cmd_function(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    await update.message.reply_text("⏳ 5 функция тапшырмасы жүктөлүүдө...")

    prompt = (
        "Python программирование боюнча 5 функция тапшырмасы бер. "
        "Ар бири мындай форматта болсун:\n"
        "N) [тапшырманын сүрөттөмөсү]\n"
        "def [функция_аты](...): pass\n"
        "Мисал: [кириш → чыгыш]\n\n"
        "Тапшырмаларды кыргыз тилинде жаз."
    )
    answer = ask_dify_text(uid, prompt)
    await update.message.reply_text(answer)
    await update.message.reply_text(
        "Функцияларды жазып жибер. Ката болгондорун кайра берем."
    )

# ─────────────────────────────────────────
# СТАТИСТИКА
# ─────────────────────────────────────────
def build_stat_text(uid: int, weekly: bool = False) -> str:
    sessions = user_data[uid]["sessions"]
    if not sessions:
        return "Азырынча тест болгон жок."

    if weekly:
        today    = datetime.now(TIMEZONE)
        cutoff   = today.replace(day=max(1, today.day - 7))
        sessions = [
            s for s in sessions
            if datetime.strptime(s["date"], "%Y-%m-%d") >= cutoff.replace(tzinfo=None)
        ]
        if not sessions:
            return "Бул жумада тест болгон жок."

    topic_stat: dict = defaultdict(lambda: {"correct": 0, "total": 0})
    for s in sessions:
        topic_stat[s["topic"]]["correct"] += s["correct"]
        topic_stat[s["topic"]]["total"]   += s["total"]

    total_c = sum(s["correct"] for s in sessions)
    total_q = sum(s["total"]   for s in sessions)
    avg     = round(total_c / total_q * 100) if total_q else 0

    title = "📊 *Жумалык статистика*" if weekly else "📊 *Сессия статистикасы*"
    lines = [title, f"Орточо балл: *{avg}%* ({total_c}/{total_q})\n"]

    good, mid, bad = [], [], []
    for topic, stat in sorted(topic_stat.items()):
        pct  = round(stat["correct"] / stat["total"] * 100) if stat["total"] else 0
        line = f"  {topic}: {stat['correct']}/{stat['total']} ({pct}%)"
        if pct >= 80:
            good.append(line)
        elif pct >= 50:
            mid.append(line)
        else:
            bad.append(line)

    if good:
        lines += ["✅ *Жакшы (80%+):*"] + good
    if mid:
        lines += ["⚠️ *Орто (50–79%):*"] + mid
    if bad:
        lines += ["❌ *Начар (<50%):*"] + bad
        worst = bad[0].strip().split(":")[0].lower().replace(" ", "")
        lines.append(f"\n💡 Сунуш: /{worst} деп жаз — ошол темадан тест өт.")

    return "\n".join(lines)

# ─────────────────────────────────────────
# ЭСКЕРТМЕЛЕР
# ─────────────────────────────────────────
async def send_reminder(app: Application):
    today = datetime.now(TIMEZONE).isoweekday()
    if today not in TEST_DAYS:
        return
    day_name = TEST_DAYS[today]
    text = (
        f"📚 Бүгүн *{day_name}* — тест күнү!\n\n"
        "Тема тандап баштагын:\n"
        "/python · /docker · /ml · /fastapi · /pytorch\n"
        "же /help — бардык командалар"
    )
    for uid in registered_users:
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Reminder failed {uid}: {e}")

async def send_weekly_stats(app: Application):
    for uid in registered_users:
        try:
            text = build_stat_text(uid, weekly=True)
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Weekly stat failed {uid}: {e}")

# ─────────────────────────────────────────
# КОМАНДАЛАР
# ─────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or "Студент"
    registered_users.add(uid)
    await update.message.reply_text(
        f"Саламатсыңбы, {name}! 👋\n\n"
        "Мен программирование экзаменине даярдоочу ботмун.\n\n"
        "Тема тандап тест баштагын:\n"
        "/python · /docker · /fastapi · /ml\n\n"
        "/help — бардык командалар\n\n"
        "⏰ Эскертме: *шейшемби, бейшемби, жекшемби* 09:00 да тест эскертмеси келет.",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    registered_users.add(update.effective_user.id)
    text = (
        "*Тема боюнча тест (15 суроо — Quiz Poll форматы):*\n"
        "/python · /postgresql · /oop · /fastapi · /django\n"
        "/streamlit · /pytorch · /docker · /aws · /ml · /dl\n"
        "/pandas · /numpy · /seaborn · /matplotlib\n\n"
        "*Башка командалар:*\n"
        "/function — 5 функция тапшырмасы\n"
        "/retry — туура эмес суроолорду кайра\n"
        "/stat — сессия статистикасы\n"
        "/weekly — жумалык статистика\n"
        "/reset — жаңы чат баштоо\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    registered_users.add(uid)
    cmd   = update.message.text.split()[0].lstrip("/").lower()
    topic = TOPICS.get(cmd)

    if not topic:
        await update.message.reply_text("Белгисиз команда. /help деп көр.")
        return

    await update.message.reply_text(f"⏳ *{topic}* тести даярдалууда...", parse_mode="Markdown")

    ok = await load_questions(uid, topic)
    if not ok:
        await update.message.reply_text("Суроо жүктөөдө ката болду. Кайра баштап көр.")
        return

    await send_next_poll(update, uid, ctx)

async def cmd_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    ok  = await load_questions(uid, user_data[uid]["current_topic"], is_retry=True)
    if not ok:
        await update.message.reply_text("Туура эмес суроо жок. Жаңы тест баштагын!")
        return
    await update.message.reply_text("🔁 Туура эмес суроолор кайра берилет...")
    await send_next_poll(update, uid, ctx)

async def cmd_stat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    await update.message.reply_text(build_stat_text(uid), parse_mode="Markdown")

async def cmd_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    await update.message.reply_text(build_stat_text(uid, weekly=True), parse_mode="Markdown")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_data[uid]["conversation_id"] = ""
    user_data[uid]["questions"]       = []
    user_data[uid]["current_q"]       = 0
    user_data[uid]["poll_map"]        = {}
    await update.message.reply_text("✅ Жаңы чат башталды. Тема тандагын!")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    registered_users.add(uid)
    text = update.message.text.strip()
    if not text:
        return
    answer = ask_dify_text(uid, text)
    await update.message.reply_text(answer)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Командалар тизмеси
    bot_commands = (
        [BotCommand(cmd, f"{name} тести") for cmd, name in TOPICS.items()]
        + [
            BotCommand("function", "5 функция тапшырмасы"),
            BotCommand("retry",    "Туура эмес суроолорду кайра"),
            BotCommand("stat",     "Сессия статистикасы"),
            BotCommand("weekly",   "Жумалык статистика"),
            BotCommand("reset",    "Жаңы чат"),
            BotCommand("help",     "Бардык командалар"),
        ]
    )

    async def post_init(app: Application):
        await app.bot.set_my_commands(bot_commands)

    app.post_init = post_init

    # Handler'лар
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("function", cmd_function))
    app.add_handler(CommandHandler("retry",    cmd_retry))
    app.add_handler(CommandHandler("stat",     cmd_stat))
    app.add_handler(CommandHandler("weekly",   cmd_weekly))
    app.add_handler(CommandHandler("reset",    cmd_reset))

    for cmd in TOPICS:
        app.add_handler(CommandHandler(cmd, cmd_topic))

    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_reminder, "cron",
        hour=REMINDER_HOUR, minute=REMINDER_MINUTE,
        args=[app], id="reminder"
    )
    scheduler.add_job(
        send_weekly_stats, "cron",
        day_of_week="sun", hour=WEEKLY_STAT_HOUR, minute=0,
        args=[app], id="weekly"
    )
    scheduler.start()

    logger.info("✅ Бот иштеп баштады!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()