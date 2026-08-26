import asyncio
import html
import logging
import math
import os
import time
import httpx
from typing import Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Poll
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    ContextTypes,
    filters
)
from telegram.error import RetryAfter, TimedOut, NetworkError

import config
import db
from parser import parse_questions_message

import sys
import io

# Fix Windows console UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        hStdIn = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(hStdIn, ctypes.byref(mode)):
            new_mode = (mode.value & ~0x0040 & ~0x0020) | 0x0080
            kernel32.SetConsoleMode(hStdIn, new_mode)
    except Exception:
        pass

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

async def global_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        logger.debug(f"[UPDATE] PollAnswer from user_id={update.poll_answer.user.id}: poll_id={update.poll_answer.poll_id}")
    elif update.message:
        logger.info(f"[UPDATE] Message from user_id={update.message.from_user.id} in chat_id={update.message.chat_id}: {update.message.text}")
    elif update.callback_query:
        logger.info(f"[UPDATE] CallbackQuery from user_id={update.callback_query.from_user.id}: data='{update.callback_query.data}'")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] Exception while handling an update: {context.error}", flush=True)
    logger.exception(context.error)

# State management for private chat Quiz creation
# user_states[user_id] = { "step": ..., "name": ..., "questions": ... }
user_states: Dict[int, Dict[str, Any]] = {}

# Active quiz sessions
active_quizzes: Dict[str, Dict[str, Any]] = {}

# Mapping poll_id -> { quiz_id, q_idx, correct_option_id, poll_start_time }
poll_id_map: Dict[str, Dict[str, Any]] = {}

def format_time(seconds: float) -> str:
    total_sec = int(round(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"

def truncate_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."

def format_quiz_to_txt(questions: list) -> str:
    blocks = []
    for q in questions:
        lines = [q["question_text"]]
        for idx, opt in enumerate(q["options"]):
            if idx == q["correct_option_id"]:
                lines.append(f"{opt} ✅")
            else:
                lines.append(opt)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

import datetime
from zoneinfo import ZoneInfo

try:
    IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")

# ==========================================
# PAUSE / RESUME / STOP HANDLERS
# ==========================================

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to pause.")
        return

    paused_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_chat = session.get("group_id")
        owner_id = session.get("user_id")
        # Allow owner of quiz or admin in group
        if chat.type != "private" and chat.id != target_chat:
            continue
        if user.id != owner_id:
            continue

        session["paused"] = True
        paused_count += 1
        try:
            await context.bot.send_message(chat_id=target_chat, text="⏸️ Quiz Paused!")
        except Exception as e:
            logger.error(f"Failed to send pause notice to chat {target_chat}: {e}")

    if paused_count == 0:
        await update.message.reply_text("❌ No active quiz running in this chat that you own.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to resume.")
        return

    resumed_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_chat = session.get("group_id")
        owner_id = session.get("user_id")
        if chat.type != "private" and chat.id != target_chat:
            continue
        if user.id != owner_id:
            continue

        session["paused"] = False
        resumed_count += 1
        try:
            await context.bot.send_message(chat_id=target_chat, text="▶️ Quiz Resumed!")
        except Exception as e:
            logger.error(f"Failed to send resume notice: {e}")

    if resumed_count == 0:
        await update.message.reply_text("❌ No active quiz running in this chat that you own.")

async def stop_command_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to stop.")
        return

    stopped_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_chat = session.get("group_id")
        owner_id = session.get("user_id")
        if chat.type != "private" and chat.id != target_chat:
            continue
        if user.id != owner_id:
            continue

        session["stopped"] = True
        stopped_count += 1
        try:
            await context.bot.send_message(chat_id=target_chat, text="⏹️ Quiz Stopped by Owner!")
        except Exception as e:
            logger.error(f"Failed to send stop notice: {e}")

    if stopped_count == 0:
        await update.message.reply_text("❌ No active quiz running in this chat that you own.")

async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to adjust speed.")
        return

    delta = 5
    if context.args and len(context.args) > 0:
        try:
            delta = int(context.args[0])
        except ValueError:
            delta = 5

    adjusted_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_chat = session.get("group_id")
        owner_id = session.get("user_id")
        if chat.type != "private" and chat.id != target_chat:
            continue
        if user.id != owner_id:
            continue

        curr_timer = session.get("timer", 15)
        new_timer = max(5, curr_timer - delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_chat,
                text=f"⚡ Quiz Speed Increased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send fast notice: {e}")

    if adjusted_count == 0:
        await update.message.reply_text("❌ No active quiz running in this chat that you own.")

async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to adjust speed.")
        return

    delta = 5
    if context.args and len(context.args) > 0:
        try:
            delta = int(context.args[0])
        except ValueError:
            delta = 5

    adjusted_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_chat = session.get("group_id")
        owner_id = session.get("user_id")
        if chat.type != "private" and chat.id != target_chat:
            continue
        if user.id != owner_id:
            continue

        curr_timer = session.get("timer", 15)
        new_timer = min(600, curr_timer + delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_chat,
                text=f"🐢 Quiz Speed Decreased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send slow notice: {e}")

    if adjusted_count == 0:
        await update.message.reply_text("❌ No active quiz running in this chat that you own.")

# ==========================================
# SCHEDULING HANDLERS
# ==========================================

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("❌ Format: /schedule <quiz_id> <HH:MM> (e.g. /schedule GGN9PV0Q9 09:00)")
        return

    quiz_id = args[0].strip()
    time_str = args[1].strip()

    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID {quiz_id} not found in database.")
        return

    if quiz_data.get("user_id") != user.id:
        await update.message.reply_text("❌ Access Denied: Aap sirf apni banai hui Quiz ko schedule kar sakte hain.")
        return

    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid time format. Use HH:MM in 24-hour format (e.g., 09:00 or 21:30).")
        return

    now = datetime.datetime.now(IST_TZ)
    scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if scheduled_dt <= now:
        scheduled_dt += datetime.timedelta(days=1)

    epoch_timestamp = scheduled_dt.timestamp()
    db.save_schedule(quiz_id, user.id, epoch_timestamp, time_str)

    time_am_pm = scheduled_dt.strftime("%I:%M %p")
    day_month = scheduled_dt.strftime("%d %b")

    delta = scheduled_dt - now
    total_seconds = int(delta.total_seconds())
    hours_left = total_seconds // 3600
    minutes_left = (total_seconds % 3600) // 60

    quiz_name = quiz_data["name"]
    announcement = (
        f"✅ Quiz Scheduled!\n\n"
        f"📝 '{quiz_name}' (ID: `{quiz_id}`)\n"
        f"🕒 {time_am_pm}, {day_month}\n"
        f"⏱️ In {hours_left}h {minutes_left}m"
    )
    await update.message.reply_text(announcement)

async def schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    schedules = db.get_active_schedules()
    user_schedules = [s for s in schedules if s.get("user_id") == user.id]
    if not user_schedules:
        await update.message.reply_text("ℹ️ Aapki koi active scheduled quiz nahi hai.")
        return

    lines = ["📅 Active Schedules:"]
    for s in user_schedules:
        quiz_id = s["quiz_id"]
        time_str = s["time_str"]
        ts = s["scheduled_timestamp"]
        dt = datetime.datetime.fromtimestamp(ts, tz=IST_TZ)
        time_am_pm = dt.strftime("%I:%M %p")
        day_month = dt.strftime("%d %b")
        lines.append(f"- ID: `{quiz_id}` | Time: `{time_str}` ({time_am_pm}, {day_month})")

    await update.message.reply_text("\n".join(lines))

async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /unschedule <quiz_id>")
        return

    quiz_id = args[0].strip()
    db.delete_schedule(quiz_id, user.id)
    await update.message.reply_text(f"✅ Schedule for Quiz ID `{quiz_id}` removed successfully.")

# ==========================================
# SCHEDULER BACKGROUND LOOP
# ==========================================

async def scheduler_loop(application):
    print("⏰ Starting background scheduler loop...", flush=True)
    try:
        while True:
            try:
                now_ts = time.time()
                schedules = await asyncio.to_thread(db.get_active_schedules)
                for s in schedules:
                    quiz_id = s["quiz_id"]
                    scheduled_ts = s["scheduled_timestamp"]
                    user_id = s.get("user_id")
                    if now_ts >= scheduled_ts:
                        print(f"⏰ Triggering scheduled quiz: {quiz_id}", flush=True)
                        await asyncio.to_thread(db.delete_schedule, quiz_id)
                        quiz_data = await asyncio.to_thread(db.get_quiz, quiz_id)
                        if quiz_data and user_id:
                            try:
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=f"⏰ Scheduled time arrived for Quiz '{quiz_data['name']}' (`{quiz_id}`)!"
                                )
                            except Exception as e:
                                logger.error(f"Error notifying schedule trigger: {e}")
            except Exception as e:
                print(f"[ERROR] Exception in scheduler_loop: {e}", flush=True)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        print("⏰ Scheduler loop stopped cleanly.", flush=True)

# ==========================================
# COMMAND HANDLERS
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user:
        return

    # Check deep link start argument (e.g. /start quiz_GG123456)
    args = context.args
    if args and len(args) > 0 and args[0].startswith("quiz_"):
        quiz_id = args[0].replace("quiz_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await update.message.reply_text("❌ Quiz not found!")
            return

        if chat.type != "private":
            logger.info(f"Triggering Quiz {quiz_id} in group chat_id={chat.id}")
            await update.message.reply_text(f"🚀 Quiz '{quiz_data['name']}' starting in this chat!")
            asyncio.create_task(run_quiz_session(context.bot, chat.id, quiz_data, update.message))
            return

        await send_quiz_created_screen(update, context, quiz_data)
        return

    if chat.type != "private":
        await update.message.reply_text("👋 Hello! Send /start in private chat with me to create your own Quizzes!")
        return

    # Private chat new quiz creation initialization
    user_states[user.id] = {
        "step": "WAITING_NAME",
        "name": "",
        "questions": []
    }
    welcome_text = (
        "🎯 **Welcome to Telegram Quiz Bot!**\n\n"
        "Aap bilkul free me apni Quiz create kar sakte hain.\n\n"
        "📝 Kripya **Quiz Ka Naam** likh kar bhejein:"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def myquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    quizzes = db.get_user_quizzes(user.id)
    if not quizzes:
        await update.message.reply_text("ℹ️ Aapne abhi tak koi Quiz nahi banayi hai.\n/start bhej kar naye Quiz banayein!")
        return

    msg_lines = [f"📚 **Aapki Quizzes** (Total: {len(quizzes)})\n"]
    keyboard = []

    for q in quizzes:
        q_id = q["quiz_id"]
        q_name = q["name"]
        q_cnt = len(q.get("questions", []))
        msg_lines.append(f"• **{q_name}** | {q_cnt} Qs | ID: `{q_id}`")
        keyboard.append([
            InlineKeyboardButton(f"🚀 Start {q_id}", callback_data=f"start_p_{q_id}"),
            InlineKeyboardButton(f"✏️ Edit", callback_data=f"ed_mgr_{q_id}"),
            InlineKeyboardButton(f"🗑️ Delete", callback_data=f"del_confirm_{q_id}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("\n".join(msg_lines), reply_markup=reply_markup, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if user.id not in user_states:
        await update.message.reply_text("ℹ️ Abhi koi creation process active nahi hai.")
        return

    del user_states[user.id]
    await update.message.reply_text("🚫 Quiz creation/editing process cancelled! /start se naya process shuru karein.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    state = user_states.get(user.id)
    if not state or state.get("step") not in ["WAITING_QUESTIONS", "WAITING_TIMER"]:
        await update.message.reply_text("❌ No active quiz creation session found. /start bhej kar quiz banayein.")
        return

    questions = state.get("questions", [])
    if len(questions) < 1:
        await update.message.reply_text("❌ Kam se kam 1 question hona zaroori hai. Questions bhejein ya /cancel karein.")
        return

    state["step"] = "WAITING_TIMER"
    await update.message.reply_text("⏳ Per-question timer (seconds me, minimum 10) enter karein (e.g. 15 or 30):")

async def handle_private_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz or /myquizzes to view your quizzes.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Iss step par .txt file expected nahi hai.")
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith('.txt'):
        await update.message.reply_text("❌ Kripya sirf valid .txt file bhejein.")
        return

    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        file_bytes = await telegram_file.download_as_bytearray()

        try:
            text_content = file_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text_content = file_bytes.decode('utf-8', errors='ignore')
            except Exception:
                text_content = file_bytes.decode('latin-1', errors='ignore')

        parsed, errors = parse_questions_message(text_content)

        if errors:
            err_msg = "⚠️ **TXT File Format Errors Found:**\n\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                err_msg += f"\n...and {len(errors) - 10} more errors."
            await update.message.reply_text(err_msg, parse_mode="Markdown")

        if not parsed:
            await update.message.reply_text("❌ File me koi valid 4-option question nahi mila. Har question me 4 options aur 1 correct ✅ mark hona chahiye.")
            return

        if step == "WAITING_QUESTIONS":
            questions = state.get("questions", [])
            questions.extend(parsed)
            state["questions"] = questions
            await update.message.reply_text(
                f"✅ **{len(parsed)} Questions Successfully Added!**\nTotal Saved: {len(questions)}\n\nAur questions txt/text se bhejein ya **/done** command bhejein.",
                parse_mode="Markdown"
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data and quiz_data.get("user_id") == user.id:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, user.id, questions)
                await update.message.reply_text(
                    f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Aur bhejein ya **/done_edit** likhein.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Quiz not found or access denied.")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Failed to process document: {e}")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Nayi Quiz banane ke liye /start bhejein, ya purani quizzes ke liye /myquizzes bhejein.")
        return

    text = update.message.text.strip() if update.message.text else ""
    step = state.get("step")

    if step == "WAITING_NAME":
        if not text:
            await update.message.reply_text("Please send a valid Quiz name.")
            return

        state["name"] = text
        state["step"] = "WAITING_QUESTIONS"
        format_guide = (
            f"✅ **Quiz Name Saved:** {text}\n\n"
            f"Ab questions bhejein! Aap:\n"
            f"1. Directly text format me send kar sakte hain:\n\n"
            f"```\n"
            f"Question Title / प्रश्न शीर्षक\n"
            f"Option 1\n"
            f"Option 2 ✅\n"
            f"Option 3\n"
            f"Option 4\n"
            f"```\n\n"
            f"2. Ya **.txt file upload** kar sakte hain.\n\n"
            f"Questions complete hone par **/done** bhejein ya cancel ke liye **/cancel** bhejein."
        )
        await update.message.reply_text(format_guide, parse_mode="Markdown")
        return

    elif step == "WAITING_QUESTIONS":
        if not text:
            return

        parsed, errors = parse_questions_message(text)
        if errors:
            err_msg = "⚠️ **Question Format Issues:**\n\n" + "\n".join(errors[:5])
            await update.message.reply_text(err_msg, parse_mode="Markdown")

        if not parsed:
            await update.message.reply_text(
                "❌ Question format invalid hai. Har question me 4 options aur 1 correct option ✅ mark hona chahiye."
            )
            return

        questions = state.get("questions", [])
        questions.extend(parsed)
        state["questions"] = questions

        await update.message.reply_text(
            f"✅ **{len(parsed)} Questions Saved!** Total: {len(questions)}\nAur questions bhejein ya **/done** type karein.",
            parse_mode="Markdown"
        )
        return

    elif step == "WAITING_TIMER":
        try:
            timer_val = int(text)
            if timer_val <= 9:
                await update.message.reply_text("⏳ Per-question timer minimum 10 seconds hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text("⏳ Valid integer timer (e.g. 15) send karein.")
            return

        state["timer"] = timer_val
        state["step"] = "WAITING_SEC_CHOICE"

        keyboard = [
            [
                InlineKeyboardButton("🟢 Yes", callback_data="create_sec_yes"),
                InlineKeyboardButton("⚪ No", callback_data="create_sec_no")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Timer set to {timer_val}s.\n\n📚 **Kya aap is Quiz me Sections divide karna chahte hain?**",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    elif step == "CREATE_SEC_COUNT":
        try:
            sec_count = int(text)
            if sec_count <= 0:
                await update.message.reply_text("❌ Minimum 1 section mandatory.")
                return
        except ValueError:
            await update.message.reply_text("❌ Valid section count (e.g. 2) send karein.")
            return

        total_q = len(state.get("questions", []))
        if sec_count > total_q:
            await update.message.reply_text(f"❌ Sections count ({sec_count}) total questions ({total_q}) se zyada nahi ho sakta.")
            return

        state["sec_total_count"] = sec_count
        state["sec_current_idx"] = 0
        state["temp_sections"] = []
        state["step"] = "CREATE_SEC_NAME"
        await update.message.reply_text("📌 Section 1 Name send karein (e.g. 🏛️ History):")
        return

    elif step == "CREATE_SEC_NAME":
        if not text:
            await update.message.reply_text("Valid Section Name send karein.")
            return

        state["curr_sec_name"] = text
        state["step"] = "CREATE_SEC_RANGE"
        idx = state.get("sec_current_idx", 0) + 1
        total_q = len(state.get("questions", []))
        await update.message.reply_text(f"🔢 Section {idx} Question Range send karein (e.g. 1-10):")
        return

    elif step == "CREATE_SEC_RANGE":
        total_q = len(state.get("questions", []))
        range_str = text.replace("to", "-").replace("TO", "-")
        nums = re.findall(r'\d+', range_str)
        if len(nums) != 2:
            await update.message.reply_text("❌ Invalid format! Range e.g. 1-10 me enter karein:")
            return

        start_q, end_q = int(nums[0]), int(nums[1])
        curr_name = state.get("curr_sec_name", "Section")
        curr_idx = state.get("sec_current_idx", 0) + 1

        if start_q < 1 or end_q > total_q or start_q > end_q:
            await update.message.reply_text(f"❌ Range 1 se {total_q} ke beech honi chahiye. Dubara bhein:")
            return

        temp_sections = state.get("temp_sections", [])
        for s in temp_sections:
            if max(s["start"], start_q) <= min(s["end"], end_q):
                await update.message.reply_text(f"❌ Range overlaps with '{s['name']}' ({s['start']}-{s['end']}). Dubara bhein:")
                return

        temp_sections.append({"name": curr_name, "start": start_q, "end": end_q})
        state["temp_sections"] = temp_sections
        state["sec_current_idx"] += 1

        if state["sec_current_idx"] < state["sec_total_count"]:
            state["step"] = "CREATE_SEC_NAME"
            next_idx = state["sec_current_idx"] + 1
            await update.message.reply_text(f"📌 Section {next_idx} Name:")
            return

        # Finish quiz creation with sections
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")
        quiz_id = db.save_quiz(
            user.id,
            state.get("name"),
            state.get("timer", 15),
            state.get("questions", []),
            creator_name=creator_name,
            sections_enabled=1,
            sections=temp_sections
        )
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    elif step == "EDIT_NAME":
        quiz_id = state.get("quiz_id")
        if not text:
            await update.message.reply_text("Valid Quiz Name send karein.")
            return
        db.update_quiz_name(quiz_id, user.id, text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Quiz name updated to `{text}`!")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_TIMER":
        quiz_id = state.get("quiz_id")
        try:
            timer_val = int(text)
            if timer_val <= 9:
                await update.message.reply_text("⏳ Minimum timer 10 seconds hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text("⏳ Valid integer timer send karein.")
            return
        db.update_quiz_timer(quiz_id, user.id, timer_val)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Quiz timer updated to `{timer_val}s`!")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_ADD_QUESTIONS":
        quiz_id = state.get("quiz_id")
        parsed, errors = parse_questions_message(text)
        if errors:
            await update.message.reply_text("\n".join(errors[:5]))

        if not parsed:
            await update.message.reply_text("❌ Question format invalid hai.")
            return

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data and quiz_data.get("user_id") == user.id:
            questions = quiz_data.get("questions", [])
            questions.extend(parsed)
            db.update_quiz_questions(quiz_id, user.id, questions)
            await update.message.reply_text(
                f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Complete hone par /done_edit bhejein."
            )
        return

async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /edit <quiz_id>")
        return

    quiz_id = args[0].strip()
    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID `{quiz_id}` not found.")
        return

    if quiz_data.get("user_id") != user.id:
        await update.message.reply_text("❌ Access Denied: Aap sirf apni banai hui Quiz ko edit kar sakte hain.")
        return

    await send_quiz_editor_screen(update, context, quiz_data)

async def done_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    state = user_states.get(user.id)
    if not state or state.get("step") != "EDIT_ADD_QUESTIONS":
        await update.message.reply_text("❌ No active editing session.")
        return

    quiz_id = state.get("quiz_id")
    del user_states[user.id]

    quiz_data = db.get_quiz(quiz_id)
    if quiz_data:
        await update.message.reply_text("✅ Questions update finished!")
        await send_quiz_editor_screen(update, context, quiz_data)

async def send_quiz_created_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    creator = quiz_data.get("creator_name", "User")

    bot_obj = context.bot
    try:
        bot_username = bot_obj.username if getattr(bot_obj, "username", None) else (await bot_obj.get_me()).username
    except Exception:
        bot_username = ""

    start_url = f"https://t.me/{bot_username}?start=quiz_{quiz_id}" if bot_username else f"https://t.me/?start=quiz_{quiz_id}"
    group_url = f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}" if bot_username else f"https://t.me/?startgroup=quiz_{quiz_id}"

    safe_name = html.escape(str(name))
    safe_creator = html.escape(str(creator))
    safe_quiz_id = html.escape(str(quiz_id))

    msg_text = (
        f"🎉 **Quiz Created Successfully!**\n\n"
        f"💳 **Name:** {safe_name}\n"
        f"#️⃣ **Questions:** {q_count}\n"
        f"⏰ **Timer:** {timer}s\n"
        f"🆔 **ID:** <code>{safe_quiz_id}</code>\n"
        f"👧 **Creator:** {safe_creator}"
    )

    keyboard = [
        [InlineKeyboardButton("🎯 Start Quiz in Chat", url=start_url)],
        [InlineKeyboardButton("🚀 Add & Run in Group", url=group_url)],
        [InlineKeyboardButton("🔗 Share Link", callback_data=f"share_{quiz_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_msg = update.callback_query.message if update.callback_query else update.message
    try:
        await target_msg.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await target_msg.reply_text(msg_text, reply_markup=reply_markup)

async def send_quiz_editor_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]

    msg_text = (
        f"🎯 **Quiz Editor**\n\n"
        f"📌 **Name:** {name}\n"
        f"🔢 **Questions:** {q_count}\n"
        f"⌚ **Timer:** {timer}s\n"
        f"🆔 **Quiz ID:** `{quiz_id}`"
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
        ],
        [
            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
            InlineKeyboardButton("📤 Export TXT", callback_data=f"ed_exp_{quiz_id}")
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="ed_close")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            await update.callback_query.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user
    data = query.data

    if data == "create_sec_no":
        state = user_states.get(user.id)
        if not state:
            return
        name = state.get("name", "Quiz")
        questions = state.get("questions", [])
        timer_val = state.get("timer", 15)
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")

        quiz_id = db.save_quiz(user.id, name, timer_val, questions, creator_name=creator_name, sections_enabled=0, sections=[])
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    if data == "create_sec_yes":
        state = user_states.get(user.id)
        if not state:
            return
        state["step"] = "CREATE_SEC_COUNT"
        await query.message.reply_text("📚 Total kitne sections banana chahte hain? (e.g. 2):")
        return

    if data.startswith("start_p_"):
        quiz_id = data.replace("start_p_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await query.message.reply_text(f"🚀 Quiz '{quiz_data['name']}' starting...")
            asyncio.create_task(run_quiz_session(context.bot, query.message.chat_id, quiz_data, query.message))
        return

    if data.startswith("ed_mgr_"):
        quiz_id = data.replace("ed_mgr_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            if quiz_data.get("user_id") != user.id:
                await query.message.reply_text("❌ Access Denied: Sirf Quiz owner edit kar sakta hai.")
                return
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    if data.startswith("del_confirm_"):
        quiz_id = data.replace("del_confirm_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data or quiz_data.get("user_id") != user.id:
            await query.message.reply_text("❌ Access Denied or Quiz not found.")
            return

        db.delete_quiz(quiz_id, user.id)
        await query.message.reply_text(f"🗑️ Quiz `{quiz_id}` deleted successfully!")
        return

    if data.startswith("share_"):
        quiz_id = data.replace("share_", "")
        bot_username = (await context.bot.get_me()).username
        share_link = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
        await query.message.reply_text(f"🔗 **Quiz Share Link:**\n{share_link}", parse_mode="Markdown")
        return

    if data.startswith("ed_exp_"):
        quiz_id = data.replace("ed_exp_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data or quiz_data.get("user_id") != user.id:
            await query.message.reply_text("❌ Quiz not found or access denied.")
            return

        questions = quiz_data.get("questions", [])
        txt_content = format_quiz_to_txt(questions)
        file_data = io.BytesIO(txt_content.encode('utf-8'))
        file_data.name = f"quiz_{quiz_id}.txt"

        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_data,
            caption=f"📤 Exported Quiz `{quiz_id}` ({len(questions)} questions)"
        )
        return

    if data.startswith("ed_name_"):
        quiz_id = data.replace("ed_name_", "")
        user_states[user.id] = {"step": "EDIT_NAME", "quiz_id": quiz_id}
        await query.message.reply_text(f"✏️ Send new Quiz Name for `{quiz_id}`:")
        return

    if data.startswith("ed_timer_"):
        quiz_id = data.replace("ed_timer_", "")
        user_states[user.id] = {"step": "EDIT_TIMER", "quiz_id": quiz_id}
        await query.message.reply_text(f"⏱️ Send new Timer in seconds for `{quiz_id}`:")
        return

    if data.startswith("ed_addq_"):
        quiz_id = data.replace("ed_addq_", "")
        user_states[user.id] = {"step": "EDIT_ADD_QUESTIONS", "quiz_id": quiz_id}
        await query.message.reply_text(f"➕ Send additional questions for `{quiz_id}`. Complete hone par /done_edit likhein.")
        return

    if data == "ed_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

# ==========================================
# QUIZ EXECUTION ENGINE
# ==========================================

def cleanup_quiz_session(quiz_id: str):
    active_quizzes.pop(quiz_id, None)
    to_delete = [p_id for p_id, info in poll_id_map.items() if info.get("quiz_id") == quiz_id]
    for p_id in to_delete:
        poll_id_map.pop(p_id, None)

async def run_quiz_session(bot, group_id: int, quiz_data: dict, status_msg=None):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    user_id = quiz_data.get("user_id")

    if quiz_id in active_quizzes:
        existing_session = active_quizzes[quiz_id]
        if existing_session.get("active", False) and not existing_session.get("stopped", False):
            if status_msg:
                try:
                    await status_msg.reply_text(f"⚠️ Quiz '{name}' is already running!")
                except Exception:
                    pass
            return

    timer = quiz_data["timer"]
    questions = quiz_data["questions"]
    total_q = len(questions)
    sec_enabled = quiz_data.get("sections_enabled", 0)
    sections = quiz_data.get("sections", [])
    sections = sorted(sections, key=lambda x: x["start"])

    active_session = {
        "quiz_id": quiz_id,
        "group_id": group_id,
        "user_id": user_id,
        "name": name,
        "timer": timer,
        "total_questions": total_q,
        "sections_enabled": sec_enabled,
        "sections": sections,
        "participants": {},
        "active": True,
        "paused": False,
        "stopped": False,
        "leaderboard_sent": False
    }
    active_quizzes[quiz_id] = active_session

    try:
        announcement_text = (
            f"🚀 **Quiz Starting!**\n\n"
            f"📜 **Quiz Name:** {name}\n"
            f"🔢 **Total Questions:** {total_q}\n"
            f"⏳ **Timer per question:** {timer}s"
        )

        try:
            await bot.send_message(chat_id=group_id, text=announcement_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send start message: {e}")

        idx = 1
        last_poll_close_time = None

        while idx <= total_q:
            if active_session.get("stopped", False):
                break
            while active_session.get("paused", False):
                if active_session.get("stopped", False):
                    break
                await asyncio.sleep(0.5)

            if active_session.get("stopped", False):
                break

            try:
                q_item = questions[idx - 1]
                raw_question = q_item["question_text"]
                options = q_item["options"]
                correct_id = q_item["correct_option_id"]

                # Handle Section transitions
                if sec_enabled == 1 and sections:
                    for s in sections:
                        if s["start"] == idx:
                            sec_msg = f"━━━━━━━━━━━━━━━━━━\n📚 **SECTION: {s['name']}**\nQuestions {s['start']} to {s['end']}\n━━━━━━━━━━━━━━━━━━"
                            try:
                                await bot.send_message(chat_id=group_id, text=sec_msg, parse_mode="Markdown")
                            except Exception:
                                pass

                # Long question / option notice
                q_text = f"[{idx}/{total_q}] {raw_question}"
                has_long_opt = any(len(opt) > 40 for opt in options)
                is_long_q = len(q_text) > 200

                if is_long_q or has_long_opt:
                    opt_prefixes = ["A", "B", "C", "D"]
                    formatted_opts = [f"  {opt_prefixes[o_i]}. {opt}" for o_i, opt in enumerate(options)]
                    long_msg_text = f"📋 **Q{idx}/{total_q}** ❓ {raw_question}\n\n" + "\n".join(formatted_opts)
                    try:
                        await bot.send_message(chat_id=group_id, text=long_msg_text, parse_mode="Markdown")
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)

                current_wait = active_session.get("timer", timer)
                open_p = min(max(5, int(current_wait)), 600)
                poll_question_text = truncate_text(q_text, 200)
                display_options = [truncate_text(opt, 40) for opt in options]

                poll_msg = None
                for attempt in range(1, 4):
                    try:
                        poll_msg = await bot.send_poll(
                            chat_id=group_id,
                            question=poll_question_text,
                            options=display_options,
                            type=Poll.QUIZ,
                            correct_option_id=correct_id,
                            is_anonymous=False,
                            open_period=open_p
                        )
                        break
                    except RetryAfter as e:
                        await asyncio.sleep(float(e.retry_after))
                    except Exception as e:
                        logger.error(f"Error sending poll Q{idx}: {e}")
                        await asyncio.sleep(1.0)

                if not poll_msg:
                    idx += 1
                    continue

                poll_created_monotonic = time.monotonic()
                poll_created_wall_time = time.time()
                p_id = poll_msg.poll.id

                poll_id_map[p_id] = {
                    "quiz_id": quiz_id,
                    "q_idx": idx,
                    "correct_option_id": correct_id,
                    "poll_start_time": poll_created_wall_time
                }

                target_end = poll_created_monotonic + current_wait
                while time.monotonic() < target_end:
                    if active_session.get("stopped", False):
                        break
                    await asyncio.sleep(0.5)

                idx += 1
            except Exception as q_err:
                logger.error(f"Exception Q{idx}: {q_err}")
                idx += 1

        await send_quiz_leaderboard(bot, group_id, active_session)

    except Exception as e:
        logger.error(f"Exception in run_quiz_session: {e}")
    finally:
        cleanup_quiz_session(quiz_id)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer:
        return

    p_id = answer.poll_id
    poll_info = poll_id_map.get(p_id)
    if not poll_info:
        return

    quiz_id = poll_info["quiz_id"]
    active_session = active_quizzes.get(quiz_id)
    if not active_session:
        return

    if active_session.get("stopped", False) or active_session.get("leaderboard_sent", False):
        return

    user = answer.user
    user_id = user.id
    selected_options = answer.option_ids

    if not selected_options:
        return

    user_selected = selected_options[0]
    q_idx = poll_info["q_idx"]
    correct_option_id = poll_info["correct_option_id"]
    poll_start_time = poll_info["poll_start_time"]
    time_taken = max(0.0, time.time() - poll_start_time)

    participants = active_session["participants"]
    if user_id not in participants:
        full_name = user.first_name + (f" {user.last_name}" if user.last_name else "")
        participants[user_id] = {
            "user_id": user_id,
            "name": full_name,
            "username": user.username or "",
            "correct": 0,
            "wrong": 0,
            "attempted_set": set(),
            "total_time": 0.0
        }

    p_data = participants[user_id]
    if q_idx not in p_data["attempted_set"]:
        p_data["attempted_set"].add(q_idx)
        p_data["total_time"] += time_taken

        if user_selected == correct_option_id:
            p_data["correct"] += 1
        else:
            p_data["wrong"] += 1

async def send_quiz_leaderboard(bot, group_id: int, session: dict):
    if session.get("leaderboard_sent", False):
        return
    session["leaderboard_sent"] = True

    participants = list(session["participants"].values())
    total_q = session["total_questions"]
    quiz_name = session.get("name", "Quiz")

    if not participants:
        await bot.send_message(
            chat_id=group_id,
            text=f"🏁 **Quiz Completed!**\n\n📝 **{quiz_name}**\n\nKoi participants nahi the."
        )
        return

    def sort_key(p):
        correct = p["correct"]
        attempted = len(p["attempted_set"])
        perf = (correct / attempted * 100.0) if attempted > 0 else 0.0
        return (-correct, -perf, p["total_time"])

    sorted_p = sorted(participants, key=sort_key)

    msg_lines = [
        "🏁 **Quiz Completed!**\n",
        f"📝 **{quiz_name}**\n",
        "🎯 **Leaderboard:**\n"
    ]
    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

    for idx, p in enumerate(sorted_p, start=1):
        name = p["name"]
        correct = p["correct"]
        wrong = p["wrong"]
        attempted = len(p["attempted_set"])
        time_str = format_time(p["total_time"])
        accuracy = (correct / total_q * 100.0) if total_q > 0 else 0.0
        badge = rank_emojis.get(idx, f"{idx}.")

        line = f"{badge} **{name}** | ✅ {correct} | ❌ {wrong} | ⏱️ {time_str} | 📊 {accuracy:.1f}%"
        msg_lines.append(line)

    await bot.send_message(chat_id=group_id, text="\n".join(msg_lines), parse_mode="Markdown")

async def post_init(application):
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    request = HTTPXRequest(
        connection_pool_size=40,
        connect_timeout=8.0,
        read_timeout=8.0,
        write_timeout=8.0,
        pool_timeout=8.0,
        httpx_kwargs={"limits": limits}
    )
    task = asyncio.create_task(scheduler_loop(application))
    application.bot_data["scheduler_task"] = task

async def post_shutdown(application):
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def main():
    db.init_db()

    token = config.BOT_TOKEN
    if not token:
        print("❌ ERROR: BOT_TOKEN is missing! Set BOT_TOKEN in .env file.")
        return

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    request = HTTPXRequest(
        connection_pool_size=40,
        connect_timeout=8.0,
        read_timeout=8.0,
        write_timeout=8.0,
        pool_timeout=8.0,
        httpx_kwargs={"limits": limits}
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, global_update_logger), group=-1)
    app.add_handler(CallbackQueryHandler(global_update_logger), group=-1)
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myquizzes", myquizzes_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("schedules", schedules_command))
    app.add_handler(CommandHandler("unschedule", unschedule_command))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("stop", stop_command_quiz))
    app.add_handler(CommandHandler("fast", fast_command))
    app.add_handler(CommandHandler("slow", slow_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("done_edit", done_edit_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_private_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_private_message))

    print("🚀 Public Telegram Quiz Bot starting...", flush=True)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "poll_answer"]
    )

if __name__ == "__main__":
    main()
