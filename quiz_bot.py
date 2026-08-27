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

# Fix Windows console UTF-8 output, unbuffered line mode, and disable QuickEdit mode to prevent process freezes on click
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        hStdIn = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(hStdIn, ctypes.byref(mode)):
            # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
            # On Windows, ENABLE_EXTENDED_FLAGS (0x0080) MUST be combined to disable QuickEdit mode successfully!
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

# Suppress spammy HTTPX/httpcore poll logs that cause Windows console buffer scrolling freezes
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def global_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        logger.debug(f"[UPDATE] PollAnswer from user_id={update.poll_answer.user.id}: poll_id={update.poll_answer.poll_id}")
    elif update.message:
        logger.info(f"[UPDATE] Message from user_id={update.message.from_user.id} in chat_id={update.message.chat_id}: {update.message.text}")
    elif update.callback_query:
        logger.info(f"[UPDATE] CallbackQuery from user_id={update.callback_query.from_user.id}: data='{update.callback_query.data}'")
    else:
        logger.debug(f"[UPDATE] Received update_id={update.update_id}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] Exception while handling an update: {context.error}", flush=True)
    logger.exception(context.error)

# State management for private chat Quiz creation
# user_states[user_id] = { "step": "WAITING_NAME" | "WAITING_QUESTIONS" | "WAITING_TIMER", "name": str, "questions": list }
user_states: Dict[int, Dict[str, Any]] = {}

# Active quiz sessions
# active_quizzes[quiz_id] = { ... }
active_quizzes: Dict[str, Dict[str, Any]] = {}

# Mapping poll_id -> { quiz_id, q_idx, correct_option_id, poll_start_time }
poll_id_map: Dict[str, Dict[str, Any]] = {}


def is_owner(user_id: int) -> bool:
    if not config.OWNER_ID or config.OWNER_ID == 0:
        logger.error("OWNER_ID is not configured or set to 0. Authorization failed.")
        return False
    return user_id == config.OWNER_ID


def is_authorized_group(chat_id: int) -> bool:
    if not config.GROUP_ID or config.GROUP_ID == 0:
        logger.error("GROUP_ID is not configured or set to 0. Group authorization failed.")
        return False
    return chat_id == config.GROUP_ID


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

# Validate timezone availability at startup (Windows needs the 'tzdata' package).
try:
    IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception as _tz_err:
    logger.critical(
        "FATAL: Timezone 'Asia/Kolkata' not found. "
        "Install the 'tzdata' package:  pip install tzdata"
    )
    raise SystemExit(
        "Missing timezone data. Run:  pip install tzdata"
    ) from _tz_err

# ==========================================
# PAUSE / RESUME / STOP HANDLERS
# ==========================================

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_owner(user.id):
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to pause.")
        return

    paused_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["paused"] = True
        paused_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="⏸️ Quiz Paused!")
        except Exception as e:
            logger.error(f"Failed to send pause notice to group {target_group}: {e}")

    if paused_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to pause.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_owner(user.id):
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to resume.")
        return

    resumed_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["paused"] = False
        resumed_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="▶️ Quiz Resumed!")
        except Exception as e:
            logger.error(f"Failed to send resume notice to group {target_group}: {e}")

    if resumed_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to resume.")


async def stop_command_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_owner(user.id):
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to stop.")
        return

    stopped_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["stopped"] = True
        stopped_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="⏹️ Quiz Stopped by Owner!")
        except Exception as e:
            logger.error(f"Failed to send stop notice to group {target_group}: {e}")

    if stopped_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to stop.")


async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_owner(user.id):
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
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        curr_timer = session.get("timer", 15)
        new_timer = max(5, curr_timer - delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_group,
                text=f"⚡ Quiz Speed Increased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send fast notice to group {target_group}: {e}")

    if adjusted_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to adjust speed.")


async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_owner(user.id):
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
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        curr_timer = session.get("timer", 15)
        new_timer = min(600, curr_timer + delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_group,
                text=f"🐢 Quiz Speed Decreased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send slow notice to group {target_group}: {e}")

    if adjusted_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to adjust speed.")


# ==========================================
# SCHEDULING HANDLERS
# ==========================================

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
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
    db.save_schedule(quiz_id, epoch_timestamp, time_str)

    time_am_pm = scheduled_dt.strftime("%I:%M %p")
    day_month = scheduled_dt.strftime("%d %b")

    delta = scheduled_dt - now
    total_seconds = int(delta.total_seconds())
    hours_left = total_seconds // 3600
    minutes_left = (total_seconds % 3600) // 60

    quiz_name = quiz_data["name"]
    announcement = (
        f"✅ Scheduled!\n\n"
        f"📝 '{quiz_name}'\n"
        f"🕒 {time_am_pm}, {day_month}\n"
        f"⏱️ In {hours_left}h {minutes_left}m"
    )

    try:
        await context.bot.send_message(chat_id=config.GROUP_ID, text=announcement)
        await update.message.reply_text(f"✅ Quiz scheduled successfully and announcement posted in group!")
    except Exception as e:
        await update.message.reply_text(f"✅ Quiz scheduled in DB, but failed to post to group: {e}")


async def schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    schedules = db.get_active_schedules()
    if not schedules:
        await update.message.reply_text("ℹ️ No active scheduled quizzes.")
        return

    lines = ["📅 Active Schedules:"]
    for s in schedules:
        quiz_id = s["quiz_id"]
        time_str = s["time_str"]
        ts = s["scheduled_timestamp"]
        dt = datetime.datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Kolkata"))
        time_am_pm = dt.strftime("%I:%M %p")
        day_month = dt.strftime("%d %b")
        lines.append(f"- ID: `{quiz_id}` | Time: `{time_str}` ({time_am_pm}, {day_month})")

    await update.message.reply_text("\n".join(lines))


async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /unschedule <quiz_id>")
        return

    quiz_id = args[0].strip()
    db.delete_schedule(quiz_id)
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
                    if now_ts >= scheduled_ts:
                        print(f"⏰ Triggering scheduled quiz: {quiz_id}", flush=True)
                        await asyncio.to_thread(db.delete_schedule, quiz_id)
                        quiz_data = await asyncio.to_thread(db.get_quiz, quiz_id)
                        if quiz_data and config.GROUP_ID != 0:
                            asyncio.create_task(run_quiz_session(application.bot, config.GROUP_ID, quiz_data))
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

    # Check for deep link arguments (e.g., /start quiz_GGN1NZBG4)
    args = context.args
    if args and len(args) > 0 and args[0].startswith("quiz_"):
        quiz_id = args[0].replace("quiz_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await update.message.reply_text("❌ Quiz not found!")
            return

        if not is_owner(user.id):
            return

        if chat.type != "private":
            logger.info(f"Triggering Quiz {quiz_id} via startgroup link in group chat_id={chat.id}")
            await update.message.reply_text(f"🚀 Quiz '{quiz_data['name']}' starting in this group ({chat.id})!")
            asyncio.create_task(run_quiz_session(context.bot, chat.id, quiz_data, update.message))
            return

        await send_quiz_created_screen(update, context, quiz_data)
        return

    if chat.type != "private":
        if not is_authorized_group(chat.id):
            await update.message.reply_text("❌ यह Quiz Bot केवल authorized group के लिए है।")
            return
        await update.message.reply_text("👋 Hello! Use this bot in private chat to create quizzes.")
        return

    # Security check for private chat creation - Silent ignore if not owner
    if not is_owner(user.id):
        return

    # Start new quiz creation flow
    user_states[user.id] = {
        "step": "WAITING_NAME",
        "name": "",
        "questions": []
    }
    await update.message.reply_text("✅ Quiz name भेजें")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if user.id not in user_states:
        await update.message.reply_text("ℹ️ No active quiz creation or editing session to cancel.")
        return

    state = user_states[user.id]
    step = state.get("step", "")

    del user_states[user.id]

    if step.startswith("EDIT_"):
        await update.message.reply_text("🚫 Quiz editing cancelled! Send /start to create or /edit to edit a Quiz.")
    else:
        await update.message.reply_text("🚫 Current Quiz creation cancelled! Send /start to begin a new Quiz.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    state = user_states.get(user.id)
    if not state or state.get("step") not in ["WAITING_QUESTIONS", "WAITING_TIMER"]:
        await update.message.reply_text("❌ No active quiz creation. Send /start to create a quiz.")
        return

    questions = state.get("questions", [])
    if len(questions) < 1:
        await update.message.reply_text("❌ At least 1 question is required. Send questions or /cancel.")
        return

    state["step"] = "WAITING_TIMER"
    await update.message.reply_text("⏳ Timer in seconds (>10)")


async def handle_private_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    if not is_owner(user.id):
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz or /edit to edit one.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Document is not expected at this step.")
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith('.txt'):
        await update.message.reply_text("❌ Please send a valid .txt file.")
        return

    try:
        # Download file
        telegram_file = await context.bot.get_file(doc.file_id)
        file_bytes = await telegram_file.download_as_bytearray()
        
        try:
            text_content = file_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text_content = file_bytes.decode('utf-8', errors='ignore')
            except Exception:
                text_content = file_bytes.decode('latin-1', errors='ignore')

        parsed = parse_questions_message(text_content)
        if not parsed:
            await update.message.reply_text("❌ No valid questions found in the file or incorrect format.")
            return

        if step == "WAITING_QUESTIONS":
            questions = state.get("questions", [])
            questions.extend(parsed)
            state["questions"] = questions
            await update.message.reply_text(
                f"✅ {len(parsed)} processed! Total: {len(questions)}\nSend more or /done"
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, questions)
                await update.message.reply_text(
                    f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Send more or type /done_edit"
                )
            else:
                await update.message.reply_text("❌ Quiz not found.")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Failed to process document: {e}")


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    if not is_owner(user.id):
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz or /edit to edit one.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Photo is not expected at this step.")
        return

    photo_file_id = update.message.photo[-1].file_id
    caption = update.message.caption.strip() if update.message.caption else ""

    if caption:
        parsed = parse_questions_message(caption)
        if not parsed:
            await update.message.reply_text(
                "❌ Photo received, but caption format is invalid!\n\nFormat:\nQuestion text\nOption 1\nOption 2 ✅\nOption 3\nOption 4"
            )
            return

        for q in parsed:
            q["photo_file_id"] = photo_file_id

        if step == "WAITING_QUESTIONS":
            questions = state.get("questions", [])
            questions.extend(parsed)
            state["questions"] = questions
            await update.message.reply_text(
                f"🖼️ Question with Photo saved! Total: {len(questions)}\nSend more or /done"
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, questions)
                await update.message.reply_text(
                    f"🖼️ Added Question with Photo! Total: {len(questions)}. Send more or type /done_edit"
                )
            else:
                await update.message.reply_text("❌ Quiz not found.")
    else:
        state["pending_photo_id"] = photo_file_id
        await update.message.reply_text(
            "🖼️ Photo received! Now send the Question text & Options for this photo."
        )


# ==========================================
# MESSAGE HANDLER FOR QUIZ CREATION
# ==========================================

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    if not is_owner(user.id):
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz.")
        return

    text = update.message.text.strip() if update.message.text else ""
    step = state.get("step")

    if step == "WAITING_NAME":
        if not text:
            await update.message.reply_text("Please send a valid Quiz name.")
            return

        state["name"] = text
        state["step"] = "WAITING_QUESTIONS"
        await update.message.reply_text(
            f"✅ Name: {text}\nQuestions भेजें, polls, testbook link, .txt या /cancel"
        )
        return

    elif step == "WAITING_QUESTIONS":
        if not text:
            return

        parsed = parse_questions_message(text)
        if not parsed:
            await update.message.reply_text(
                "❌ Invalid question format!\n\nFormat:\nQuestion text\nOption 1\nOption 2 ✅\nOption 3\nOption 4"
            )
            return

        pending_photo_id = state.pop("pending_photo_id", None)
        if pending_photo_id:
            for q in parsed:
                q["photo_file_id"] = pending_photo_id

        questions = state.get("questions", [])
        questions.extend(parsed)
        state["questions"] = questions

        await update.message.reply_text(f"✅ {len(questions)} saved! Send more or /done")
        return

    elif step == "WAITING_TIMER":
        try:
            timer_val = int(text)
            if timer_val <= 10:
                await update.message.reply_text("⏳ Timer in seconds (>10)")
                return
        except ValueError:
            await update.message.reply_text("⏳ Timer in seconds (>10)")
            return

        state["timer"] = timer_val
        state["step"] = "WAITING_SEC_CHOICE"

        q_count = len(state.get("questions", []))
        keyboard = [
            [
                InlineKeyboardButton("🟢 Yes", callback_data="create_sec_yes"),
                InlineKeyboardButton("⚪ No", callback_data="create_sec_no")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Questions completed\n\n"
            f"📚 Section Quiz?",
            reply_markup=reply_markup
        )
        return

    elif step == "CREATE_SEC_COUNT":
        try:
            sec_count = int(text)
            if sec_count <= 0:
                await update.message.reply_text("❌ Total sections must be at least 1.")
                return
        except ValueError:
            await update.message.reply_text("❌ Valid number send karein (e.g. 2).")
            return

        total_q = len(state.get("questions", []))
        if sec_count > total_q:
            await update.message.reply_text(f"❌ Sections count ({sec_count}) total questions ({total_q}) se zyada nahi ho sakta.")
            return

        state["sec_total_count"] = sec_count
        state["sec_current_idx"] = 0
        state["temp_sections"] = []
        state["step"] = "CREATE_SEC_NAME"
        await update.message.reply_text("📌 Section 1 Name:\nExample: 🏛️ History")
        return

    elif step == "CREATE_SEC_NAME":
        if not text:
            await update.message.reply_text("Please send a valid Section Name.")
            return

        state["curr_sec_name"] = text
        state["step"] = "CREATE_SEC_RANGE"
        idx = state.get("sec_current_idx", 0) + 1
        total_q = len(state.get("questions", []))
        await update.message.reply_text(
            f"🔢 Section {idx} Range:\nExample: 1-50"
        )
        return

    elif step == "CREATE_SEC_RANGE":
        import re
        total_q = len(state.get("questions", []))
        range_str = text.replace("to", "-").replace("TO", "-")
        nums = re.findall(r'\d+', range_str)
        if len(nums) != 2:
            await update.message.reply_text(f"❌ Range format invalid! Standard format send karein (e.g. 1-50):")
            return

        start_q, end_q = int(nums[0]), int(nums[1])
        curr_name = state.get("curr_sec_name", "Section")
        curr_idx = state.get("sec_current_idx", 0) + 1

        if start_q < 1 or end_q > total_q or start_q > end_q:
            await update.message.reply_text(
                f"❌ Invalid section range!\n"
                f"Start Q1 se kam nahi ho sakta aur End Q{total_q} se zyada nahi ho sakta.\n\n"
                f"Section {curr_idx} Range dubara send karein (e.g. 1-50):"
            )
            return

        # Range Overlap Validation
        temp_sections = state.get("temp_sections", [])
        overlap_found = False
        ov_sec_name = ""
        ov_range = ""
        for s in temp_sections:
            s_start = s["start"]
            s_end = s["end"]
            if max(s_start, start_q) <= min(s_end, end_q):
                overlap_found = True
                ov_sec_name = s["name"]
                ov_range = f"{s_start}-{s_end}"
                break

        if overlap_found:
            await update.message.reply_text(
                f"❌ Section ranges overlap!\n"
                f"{ov_sec_name}: {ov_range}\n"
                f"{curr_name}: {start_q}-{end_q}\n\n"
                f"Section {curr_idx} Range dubara send karein:"
            )
            return

        # Add section
        temp_sections.append({"name": curr_name, "start": start_q, "end": end_q})
        state["temp_sections"] = temp_sections
        state["sec_current_idx"] += 1

        if state["sec_current_idx"] < state["sec_total_count"]:
            state["step"] = "CREATE_SEC_NAME"
            next_idx = state["sec_current_idx"] + 1
            await update.message.reply_text(f"📌 Section {next_idx} Name:")
            return

        # All sections configured! Show Summary Confirmation Screen
        temp_sections.sort(key=lambda x: x["start"])
        sec_summary_lines = ["📚 SECTION SETUP\n"]
        for s in temp_sections:
            sec_summary_lines.append(f"{s['name']}")

        sec_summary_lines.append("\n━━━━━━━━━━━━━━━━━━")
        sec_summary_lines.append(f"❓ Total Questions: {total_q}")
        sec_summary_lines.append(f"📚 Total Sections: {len(temp_sections)}")
        sec_summary_lines.append("━━━━━━━━━━━━━━━━━━\n")

        msg_text = "\n".join(sec_summary_lines)
        keyboard = [
            [
                InlineKeyboardButton("💾 Save Quiz", callback_data="create_sec_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="create_sec_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(msg_text, reply_markup=reply_markup)
        state["step"] = "CREATE_SEC_CONFIRM"
        return

    elif step == "EDIT_NAME":
        quiz_id = state.get("quiz_id")
        if not text:
            await update.message.reply_text("Please send a valid Quiz name.")
            return
        db.update_quiz_name(quiz_id, text)
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
            if timer_val <= 10:
                await update.message.reply_text("⏳ Timer in seconds (>10)")
                return
        except ValueError:
            await update.message.reply_text("⏳ Timer in seconds (>10)")
            return
        db.update_quiz_timer(quiz_id, timer_val)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Quiz timer updated to `{timer_val}s`!")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_ADD_QUESTIONS":
        quiz_id = state.get("quiz_id")
        if not text:
            return
        parsed = parse_questions_message(text)
        if not parsed:
            await update.message.reply_text("❌ Invalid question format!")
            return

        pending_photo_id = state.pop("pending_photo_id", None)
        if pending_photo_id:
            for q in parsed:
                q["photo_file_id"] = pending_photo_id

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            questions = quiz_data.get("questions", [])
            questions.extend(parsed)
            db.update_quiz_questions(quiz_id, questions)
            await update.message.reply_text(
                f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Send more or type /done_edit"
            )
        return

    elif step == "ADD_SEC_NAME":
        quiz_id = state.get("quiz_id")
        if not text:
            await update.message.reply_text("Please send a valid Section Name.")
            return
        state["sec_name"] = text
        state["step"] = "ADD_SEC_START"
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        await update.message.reply_text(f"🔢 Start Question Number send karein (1 to {total_q}):")
        return

    elif step == "ADD_SEC_START":
        quiz_id = state.get("quiz_id")
        sec_name = state.get("sec_name")
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        try:
            start_q = int(text)
            if start_q < 1 or start_q > total_q:
                await update.message.reply_text(f"❌ Start Question Number 1 aur {total_q} ke beech hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text(f"❌ Valid number send karein (1 to {total_q}).")
            return
        state["start_q"] = start_q
        state["step"] = "ADD_SEC_END"
        await update.message.reply_text(f"🔢 End Question Number send karein ({start_q} to {total_q}):")
        return

    elif step == "ADD_SEC_END":
        quiz_id = state.get("quiz_id")
        sec_name = state.get("sec_name")
        start_q = state.get("start_q")
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        try:
            end_q = int(text)
            if end_q < start_q or end_q > total_q:
                await update.message.reply_text(f"❌ End Question Number {start_q} aur {total_q} ke beech hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text(f"❌ Valid number send karein ({start_q} to {total_q}).")
            return

        del user_states[user.id]

        # Range Overlap Validation
        existing_sections = quiz_data.get("sections", [])
        overlap_found = False
        overlapping_sec_name = ""
        overlap_range = ""
        for s in existing_sections:
            s_start = s["start"]
            s_end = s["end"]
            if max(s_start, start_q) <= min(s_end, end_q):
                overlap_found = True
                overlapping_sec_name = s["name"]
                overlap_range = f"{s_start}–{s_end}"
                break

        if overlap_found:
            await update.message.reply_text(
                f"❌ Section ranges overlap!\n\n"
                f"Existing: {overlapping_sec_name} ({overlap_range})\n"
                f"Attempted: {sec_name} ({start_q}–{end_q})\n\n"
                f"Please re-add section with a non-overlapping range."
            )
            await send_section_manager_screen(update, context, quiz_data)
            return

        # Valid! Add section and sort by start question
        new_sec = {"name": sec_name, "start": start_q, "end": end_q}
        existing_sections.append(new_sec)
        existing_sections.sort(key=lambda x: x["start"])
        db.update_quiz_sections(quiz_id, existing_sections)
        db.update_quiz_sections_enabled(quiz_id, 1)

        await update.message.reply_text(f"✅ Section '{sec_name}' (Q{start_q}–Q{end_q}) added successfully!")
        quiz_data["sections"] = existing_sections
        quiz_data["sections_enabled"] = 1
        await send_section_manager_screen(update, context, quiz_data)
        return


async def send_quiz_created_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    creator = quiz_data.get("creator_name", "MAHI 💗")

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
        f"Quiz Created! 💬\n\n"
        f"💳 Name: {safe_name}\n"
        f"#️⃣ Questions: {q_count}\n"
        f"⏰ Timer: {timer}s\n"
        f"🆔 ID: <code>{safe_quiz_id}</code>\n"
        f"💰 Type: free\n"
        f"☠️ -ve: 0.00\n"
        f"👧 Creator: {safe_creator}"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎯 Start", url=start_url)
        ],
        [
            InlineKeyboardButton("🚀 Group", url=group_url)
        ],
        [
            InlineKeyboardButton("🔗 Share", callback_data=f"share_{quiz_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_msg = update.callback_query.message if update.callback_query else update.message
    try:
        await target_msg.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to send quiz created screen with HTML parse_mode: {e}, falling back to plain text")
        plain_msg = (
            f"Quiz Created! 💬\n\n"
            f"💳 Name: {name}\n"
            f"#️⃣ Questions: {q_count}\n"
            f"⏰ Timer: {timer}s\n"
            f"🆔 ID: {quiz_id}\n"
            f"💰 Type: free\n"
            f"☠️ -ve: 0.00\n"
            f"👧 Creator: {creator}"
        )
        await target_msg.reply_text(plain_msg, reply_markup=reply_markup)


async def send_quiz_editor_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    sec_enabled = quiz_data.get("sections_enabled", 0)
    sections = quiz_data.get("sections", [])
    sec_status_str = f"🟢 Enabled ({len(sections)} Sections)" if sec_enabled == 1 else "⚪ Disabled"

    msg_text = (
        f"🎯 Quiz Editor\n\n"
        f"📌 Name: {name}\n"
        f"🔢 Questions: {q_count}\n"
        f"⌚ Timer: {timer}s\n"
        f"📚 Sections: {sec_status_str}\n"
        f"💰 Type: Free\n"
        f"➖ Negative: 0\n"
        f"📢 Promo: ❌ None"
    )

    toggle_btn_text = "📚 Sections: 🟢 Enabled" if sec_enabled == 1 else "📚 Sections: ⚪ Disabled"

    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
        ],
        [
            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
            InlineKeyboardButton("🔀 Shuffle", callback_data=f"ed_shuf_{quiz_id}")
        ],
        [
            InlineKeyboardButton(toggle_btn_text, callback_data=f"sec_tog_{quiz_id}")
        ],
        [
            InlineKeyboardButton("📚 Manage Sections", callback_data=f"sec_mgr_{quiz_id}")
        ],
        [
            InlineKeyboardButton("📤 Export", callback_data=f"ed_exp_{quiz_id}")
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="ed_close")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup)
        except Exception:
            await update.callback_query.message.reply_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)


async def send_section_manager_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    q_count = len(quiz_data["questions"])
    sections = quiz_data.get("sections", [])

    lines = [
        f"📚 QUIZ SECTIONS (Total Questions: {q_count})\n"
    ]
    if not sections:
        lines.append("ℹ️ No sections configured yet.\nClick '➕ Add Section' to add a section.")
    else:
        for idx, s in enumerate(sections, start=1):
            lines.append(f"{s['name']} → Q{s['start']}–Q{s['end']}")

    msg_text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Section", callback_data=f"sec_add_{quiz_id}"),
            InlineKeyboardButton("🗑️ Clear Sections", callback_data=f"sec_clr_{quiz_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back to Editor", callback_data=f"sec_back_{quiz_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup)
        except Exception:
            await update.callback_query.message.reply_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /edit <quiz_id> (e.g. /edit GGNRUOE6F)")
        return

    quiz_id = args[0].strip()
    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID {quiz_id} not found in database.")
        return

    await send_quiz_editor_screen(update, context, quiz_data)


async def done_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    state = user_states.get(user.id)
    if not state or state.get("step") != "EDIT_ADD_QUESTIONS":
        await update.message.reply_text("❌ No active question editing session.")
        return

    quiz_id = state.get("quiz_id")
    del user_states[user.id]

    quiz_data = db.get_quiz(quiz_id)
    if quiz_data:
        await update.message.reply_text("✅ Questions update finished!")
        await send_quiz_editor_screen(update, context, quiz_data)
    else:
        await update.message.reply_text("✅ Questions editing finished!")


# ==========================================
# CALLBACK QUERY HANDLER
# ==========================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"[API TIMEOUT/ERROR] query.answer() non-fatal exception: {e}")

    user = query.from_user
    logger.info(f"Callback received: {query.data} from user_id={user.id}")

    if not is_owner(user.id):
        return

    data = query.data

    if data == "create_sec_no":
        state = user_states.get(user.id)
        if not state:
            return
        name = state.get("name", "Quiz")
        questions = state.get("questions", [])
        timer_val = state.get("timer", 15)
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")

        quiz_id = db.save_quiz(name, timer_val, questions, creator_name=creator_name, sections_enabled=0, sections=[])
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    if data == "create_sec_yes":
        state = user_states.get(user.id)
        if not state:
            return
        state["step"] = "CREATE_SEC_COUNT"
        await query.message.reply_text("📚 How many sections?")
        return

    if data == "create_sec_confirm":
        state = user_states.get(user.id)
        if not state:
            return
        name = state.get("name", "Quiz")
        questions = state.get("questions", [])
        timer_val = state.get("timer", 15)
        temp_sections = state.get("temp_sections", [])
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")

        quiz_id = db.save_quiz(name, timer_val, questions, creator_name=creator_name, sections_enabled=1, sections=temp_sections)
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    if data == "create_sec_cancel":
        if user.id in user_states:
            del user_states[user.id]
        await query.message.reply_text("🚫 Quiz creation cancelled!")
        return

    if data.startswith("share_"):
        quiz_id = data.replace("share_", "")
        bot_username = (await context.bot.get_me()).username
        share_link = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
        await query.message.reply_text(
            f"🔗 Share link for Quiz ({quiz_id}):\n{share_link}\n\n"
            f"Note: This Quiz will execute ONLY in the authorized group."
        )
        return

    if data.startswith("start_g_"):
        quiz_id = data.replace("start_g_", "").strip()
        quiz_data = db.get_quiz(quiz_id)

        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        if not is_owner(user.id):
            await query.message.reply_text("❌ Access Denied: Sirf Owner hi group me quiz launch kar sakta hai.")
            return

        user_states[user.id] = {
            "step": "WAITING_NATIVE_GROUP",
            "launch_quiz_id": quiz_id
        }

        request_chat_button = KeyboardButton(
            text="🎯 Select Group for Quiz",
            request_chat=KeyboardButtonRequestChat(
                request_id=1,
                chat_is_channel=False,
                bot_is_member=True
            )
        )
        reply_markup = ReplyKeyboardMarkup(
            [[request_chat_button]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await query.message.reply_text(
            f"👇 Neeche **'🎯 Select Group for Quiz'** button par click karke Telegram se target Group choose karein jahan Quiz `{quiz_id}` launch karni hai:",
            reply_markup=reply_markup
        )
        return

    if data.startswith("start_p_"):
        quiz_id = data.replace("start_p_", "").strip()
        quiz_data = db.get_quiz(quiz_id)

        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        if config.GROUP_ID == 0:
            await query.message.reply_text("❌ GROUP_ID configuration missing in environment variable.")
            return

        target_group = config.GROUP_ID
        logger.info(f"Triggering Quiz {quiz_id} in GROUP_ID={target_group}")

        # Start Async Task for running the Quiz in the default authorized group
        asyncio.create_task(run_quiz_session(context.bot, target_group, quiz_data, query.message))
        await query.message.reply_text(f"🚀 Quiz '{quiz_data['name']}' starting in default group ({target_group})!")
        return

    if data.startswith("ed_exp_"):
        quiz_id = data.replace("ed_exp_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        questions = quiz_data.get("questions", [])
        if not questions:
            await query.message.reply_text("❌ No questions found in this quiz to export.")
            return

        try:
            txt_content = format_quiz_to_txt(questions)
            file_data = io.BytesIO(txt_content.encode('utf-8'))
            file_data.name = f"quiz_{quiz_id}.txt"
            
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_data,
                caption=f"📤 Exported ({len(questions)} questions)"
            )
        except Exception as e:
            logger.exception(e)
            await query.message.reply_text(f"❌ Failed to export quiz: {e}")
        return

    if data.startswith("ed_name_"):
        quiz_id = data.replace("ed_name_", "")
        user_states[user.id] = {"step": "EDIT_NAME", "quiz_id": quiz_id}
        await query.message.reply_text(f"✏️ Send new Quiz Name for `{quiz_id}`:")
        return

    if data.startswith("ed_timer_"):
        quiz_id = data.replace("ed_timer_", "")
        user_states[user.id] = {"step": "EDIT_TIMER", "quiz_id": quiz_id}
        await query.message.reply_text(f"⏱️ Send new Timer in seconds (>10) for `{quiz_id}`:")
        return

    if data.startswith("ed_addq_"):
        quiz_id = data.replace("ed_addq_", "")
        user_states[user.id] = {"step": "EDIT_ADD_QUESTIONS", "quiz_id": quiz_id}
        await query.message.reply_text(
            f"➕ Send additional questions for `{quiz_id}` in standard format.\nWhen done sending, type /done_edit"
        )
        return

    if data.startswith("ed_shuf_"):
        quiz_id = data.replace("ed_shuf_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            import random
            questions = quiz_data["questions"]
            random.shuffle(questions)
            db.update_quiz_questions(quiz_id, questions)
            await query.message.reply_text(f"🔀 Questions shuffled for Quiz `{quiz_id}`!")
            quiz_data["questions"] = questions
            await send_quiz_editor_screen(update, context, quiz_data)
        else:
            await query.message.reply_text("❌ Quiz not found.")
        return

    if data == "ed_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("sec_tog_"):
        quiz_id = data.replace("sec_tog_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            new_state = 1 if quiz_data.get("sections_enabled", 0) == 0 else 0
            db.update_quiz_sections_enabled(quiz_id, new_state)
            quiz_data["sections_enabled"] = new_state
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    if data.startswith("sec_mgr_"):
        quiz_id = data.replace("sec_mgr_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_section_manager_screen(update, context, quiz_data)
        return

    if data.startswith("sec_back_"):
        quiz_id = data.replace("sec_back_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    if data.startswith("sec_clr_"):
        quiz_id = data.replace("sec_clr_", "")
        db.update_quiz_sections(quiz_id, [])
        await query.message.reply_text(f"🗑️ All sections cleared for Quiz `{quiz_id}`.")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_section_manager_screen(update, context, quiz_data)
        return

    if data.startswith("sec_add_"):
        quiz_id = data.replace("sec_add_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return
        user_states[user.id] = {"step": "ADD_SEC_NAME", "quiz_id": quiz_id}
        await query.message.reply_text("📌 Section Name send karein (e.g. 🏛️ History):")
        return


async def chat_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    chat_shared = update.message.chat_shared
    if not chat_shared:
        return

    selected_group_id = chat_shared.chat_id
    state = user_states.get(user.id, {})
    quiz_id = state.get("launch_quiz_id")

    if not quiz_id:
        await update.message.reply_text(
            "❌ Quiz session expired or missing. Please click '🎯 Group' again on the Quiz screen.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(
            f"❌ Quiz ID `{quiz_id}` not found in database.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # Clean up waiting state
    if user.id in user_states:
        del user_states[user.id]

    logger.info(f"Triggering Quiz {quiz_id} in selected group_id={selected_group_id}")
    await update.message.reply_text(
        f"🚀 Quiz '{quiz_data['name']}' starting in selected group (`{selected_group_id}`)!",
        reply_markup=ReplyKeyboardRemove()
    )
    asyncio.create_task(run_quiz_session(context.bot, selected_group_id, quiz_data, update.message))


# ==========================================
# QUIZ EXECUTION ENGINE
# ==========================================

def cleanup_quiz_session(quiz_id: str):
    active_quizzes.pop(quiz_id, None)
    to_delete = [p_id for p_id, info in poll_id_map.items() if info.get("quiz_id") == quiz_id]
    for p_id in to_delete:
        poll_id_map.pop(p_id, None)


async def run_quiz_session(bot, group_id: int, quiz_data: dict, status_msg=None):
    if quiz_engine_bot is not None:
        bot = quiz_engine_bot

    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]

    # Protection against duplicate running timers/schedulers
    if quiz_id in active_quizzes:
        existing_session = active_quizzes[quiz_id]
        if existing_session.get("active", False) and not existing_session.get("stopped", False):
            logger.warning(f"Quiz {quiz_id} is already active. Preventing duplicate launch.")
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
    if len(sections) > 0:
        sec_enabled = 1
    sections = sorted(sections, key=lambda x: x["start"])

    # Initialize active quiz tracking
    active_session = {
        "quiz_id": quiz_id,
        "group_id": group_id,
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
        # 1. Send announcement in Group with Retry for transient network timeouts
        announcement_sent = False
        announcement_text = (
            f"🚀 Quiz Starting!\n\n"
            f"📜 Quiz Name: {name}\n"
            f"🔢 Total Questions: {total_q}\n"
            f"⏳ Time per question: {timer}s"
        )

        for ann_attempt in range(1, 4):
            try:
                logger.info(f"Sending start announcement to group {group_id}... (attempt {ann_attempt}/3)")
                await bot.send_message(chat_id=group_id, text=announcement_text)
                announcement_sent = True
                break
            except Exception as ann_err:
                logger.warning(f"[API TIMEOUT] send_message (announcement) attempt {ann_attempt}/3 failed: {ann_err}")
                if ann_attempt < 3:
                    await asyncio.sleep(1.0)

        if not announcement_sent:
            logger.error(f"Failed to send quiz start message in group {group_id} after 3 attempts.")
            if status_msg:
                try:
                    await status_msg.reply_text(
                        f"❌ Failed to start quiz in Group ({group_id}): Connection/Timeout error after 3 attempts."
                    )
                except Exception:
                    pass
            cleanup_quiz_session(quiz_id)
            return

        # 2. Iterate through questions using a while loop to ensure index advances only after successful poll creation
        idx = 1
        last_poll_close_time = None

        while idx <= total_q:
            t_loop_start = time.monotonic()
            if last_poll_close_time is not None:
                gap_ms = (t_loop_start - last_poll_close_time) * 1000.0
                print(f"[QUIZ TIMING] Q{idx} loop started | Gap from last poll finish: {gap_ms:.2f}ms", flush=True)

            if active_session.get("stopped", False):
                break
            while active_session.get("paused", False):
                if active_session.get("stopped", False):
                    break
                await asyncio.sleep(0.5)

            if active_session.get("stopped", False):
                break

            try:
                t_prep_start = time.monotonic()
                q_item = questions[idx - 1]
                raw_question = q_item["question_text"]
                options = q_item["options"]
                correct_id = q_item["correct_option_id"]
                print(f"[QUIZ TIMING] Q{idx} question preparation: {((time.monotonic() - t_prep_start) * 1000.0):.2f}ms", flush=True)

                # Section Transition check before question starts
                t_sec_start = time.monotonic()
                if sec_enabled == 1 and sections:
                    match_sec_idx = None
                    for s_i, s in enumerate(sections):
                        if s["start"] == idx:
                            match_sec_idx = s_i
                            break

                    if match_sec_idx is not None:
                        curr_sec = sections[match_sec_idx]
                        if match_sec_idx == 0:
                            sec_msg = (
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{curr_sec['name']}\n"
                                f"📚 SECTION 1\n"
                                f"━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 GET READY!\n"
                                f"━━━━━━━━━━━━━━━━━━"
                            )
                        else:
                            prev_sec = sections[match_sec_idx - 1]
                            q_count_in_prev_sec = prev_sec["end"] - prev_sec["start"] + 1
                            sec_msg = (
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{prev_sec['name']} COMPLETED [✅] {q_count_in_prev_sec} Questions Completed\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{curr_sec['name']} NEXT SECTION\n"
                                f"📚 Questions {curr_sec['start']}–{curr_sec['end']}\n"
                                f"━━━━━━━━━━━━━━━━━━"
                            )
                        try:
                            await bot.send_message(chat_id=group_id, text=sec_msg)
                        except Exception as e:
                            logger.error(f"Error sending section announcement: {e}")
                print(f"[QUIZ TIMING] Q{idx} section check: {((time.monotonic() - t_sec_start) * 1000.0):.2f}ms", flush=True)

                # Send question photo if available
                photo_file_id = q_item.get("photo_file_id")
                if photo_file_id:
                    for photo_attempt in range(1, 4):
                        if active_session.get("stopped", False):
                            break
                        try:
                            await bot.send_photo(chat_id=group_id, photo=photo_file_id)
                            await asyncio.sleep(0.2)
                            break
                        except Exception as pe:
                            logger.error(f"Error sending photo for Q{idx} (attempt {photo_attempt}/3): {pe}")
                            if photo_attempt < 3:
                                await asyncio.sleep(0.5)

                # Handle full question and options ONLY if exceeding Poll card capacity (200 chars question / 40 chars options)
                t_long_start = time.monotonic()
                q_text = f"[{idx}/{total_q}] {raw_question}"
                has_long_opt = any(len(opt) > 40 for opt in options)
                is_long_q = len(q_text) > 200
                if is_long_q or has_long_opt:
                    if has_long_opt:
                        opt_prefixes = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
                        formatted_opts = []
                        for o_i, opt in enumerate(options):
                            pref = opt_prefixes[o_i] if o_i < len(opt_prefixes) else f"{o_i+1}"
                            formatted_opts.append(f"  {pref}. {opt}")
                        
                        long_msg_text = (
                            f"📋 Q{idx}/{total_q}\n"
                            f"❓ {raw_question}\n\n"
                            f"🔤 Options:\n" +
                            "\n".join(formatted_opts)
                        )
                    else:
                        long_msg_text = f"📋 Q{idx}/{total_q} ❓ {raw_question}"
                    # Retry sending long message up to 5 times if rate-limited or transient network error occurs
                    for msg_attempt in range(1, 6):
                        if active_session.get("stopped", False):
                            break
                        try:
                            await bot.send_message(chat_id=group_id, text=long_msg_text)
                            print(f"[QUIZ TIMING] Q{idx} full question and options sent: {((time.monotonic() - t_long_start) * 1000.0):.2f}ms", flush=True)
                            break
                        except RetryAfter as e:
                            retry_wait = float(e.retry_after)
                            logger.warning(f"Rate limited on long message Q{idx}. Waiting {retry_wait}s...")
                            await asyncio.sleep(retry_wait)
                        except Exception as e:
                            logger.error(f"Error sending long question message Q{idx} (attempt {msg_attempt}/5): {e}")
                            if msg_attempt < 5:
                                await asyncio.sleep(0.3)
                    
                    # Short breather delay to prevent hitting group rate limit between message & poll
                    await asyncio.sleep(0.1)

                # Send poll with robust retry & RetryAfter handling
                poll_msg = None
                poll_attempts = 7
                current_wait = active_session.get("timer", timer)
                open_p = min(max(5, int(current_wait)), 600)
                q_text = f"[{idx}/{total_q}] {raw_question}"
                poll_question_text = truncate_text(q_text, 200)
                display_options = [truncate_text(opt, 40) for opt in options]

                t_poll_create_start = time.monotonic()
                print(f"[QUIZ TIMING] Q{idx} poll creation started", flush=True)

                for attempt in range(1, poll_attempts + 1):
                    if active_session.get("stopped", False):
                        break
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
                        retry_wait = float(e.retry_after)
                        logger.warning(f"[quiz_id={quiz_id}] Telegram Rate Limit (429) for Q{idx}. Waiting {retry_wait}s (attempt {attempt}/{poll_attempts})...")
                        await asyncio.sleep(retry_wait)
                    except Exception as e:
                        logger.error(f"[quiz_id={quiz_id}] Error sending poll Q{idx} (attempt {attempt}/{poll_attempts}): {e}")
                        if attempt < poll_attempts:
                            backoff = min(2.0, 0.3 * attempt)
                            await asyncio.sleep(backoff)
                        else:
                            logger.error(f"[quiz_id={quiz_id}] Failed to send poll for Q{idx} after {poll_attempts} attempts. Skipping question.")
                            try:
                                await bot.send_message(chat_id=group_id, text=f"⚠️ Question {idx} skipped (network/API error). Moving to next question...")
                            except Exception:
                                pass
                            poll_msg = None
                            break

                if active_session.get("stopped", False):
                    break

                if not poll_msg:
                    # Skip to next question smoothly if poll failed to send
                    idx += 1
                    continue

                # Capture high-resolution monotonic timer ONLY AFTER successful poll creation
                poll_created_monotonic = time.monotonic()
                poll_created_wall_time = time.time()
                rtt_sec = poll_created_monotonic - t_poll_create_start

                # Subtract half of the API RTT so local timer expires at the exact millisecond Telegram server auto-closes the poll
                effective_wait = max(0.5, current_wait - (rtt_sec / 2.0))
                target_end_monotonic = poll_created_monotonic + effective_wait

                poll_creation_ms = rtt_sec * 1000.0
                print(f"[QUIZ TIMING] Q{idx} poll created: {poll_creation_ms:.2f}ms | RTT offset: {(rtt_sec * 500.0):.2f}ms | Effective wait: {effective_wait:.3f}s", flush=True)

                # Register poll in map for user answer score tracking
                p_id = poll_msg.poll.id
                poll_id_map[p_id] = {
                    "quiz_id": quiz_id,
                    "q_idx": idx,
                    "correct_option_id": correct_id,
                    "poll_start_time": poll_created_wall_time
                }

                # Adaptive precision timer loop (zero-drift, non-busy with safety hard limit)
                loop_max_end = target_end_monotonic + 5.0
                while True:
                    if active_session.get("stopped", False):
                        break
                    if active_session.get("paused", False):
                        await asyncio.sleep(0.5)
                        continue

                    now_m = time.monotonic()
                    if now_m >= target_end_monotonic or now_m >= loop_max_end:
                        break

                    remaining = target_end_monotonic - now_m
                    if remaining > 1.0:
                        await asyncio.sleep(min(0.5, remaining - 0.2))
                    elif remaining > 0.1:
                        await asyncio.sleep(min(0.1, remaining - 0.02))
                    else:
                        await asyncio.sleep(max(0.005, remaining))

                last_poll_close_time = time.monotonic()
                print(f"[QUIZ TIMING] Q{idx} poll ended", flush=True)

                if active_session.get("stopped", False):
                    try:
                        await bot.stop_poll(chat_id=group_id, message_id=poll_msg.message_id)
                    except Exception:
                        pass
                    break

                t_score_start = time.monotonic()
                # Result processing and score updates happen asynchronously via PollAnswerHandler
                print(f"[QUIZ TIMING] Q{idx} result processing & score update: {((time.monotonic() - t_score_start) * 1000.0):.2f}ms", flush=True)

                idx += 1
            except Exception as q_err:
                logger.error(f"[quiz_id={quiz_id}] Exception while handling Q{idx}: {q_err}")
                try:
                    await bot.send_message(chat_id=group_id, text=f"⚠️ Question {idx} me error aaya: {q_err}. Agla question start ho raha hai...")
                except Exception:
                    pass
                idx += 1  # Always advance to next question so quiz NEVER hangs

        # 3. Finalize Quiz and Send Leaderboard
        if active_session.get("stopped", False):
            await bot.send_message(chat_id=group_id, text="⏹️ Quiz Stopped! Calculating results for attempted questions...")
        
        await send_quiz_leaderboard(bot, group_id, active_session)

    except Exception as e:
        logger.error(f"[quiz_id={quiz_id}] Exception in run_quiz_session: {e}")
        # Try to send leaderboard on crash if not already sent
        try:
            await send_quiz_leaderboard(bot, group_id, active_session)
        except Exception as le:
            logger.error(f"[quiz_id={quiz_id}] Failed to send leaderboard on crash: {le}")
    finally:
        cleanup_quiz_session(quiz_id)


# ==========================================
# POLL ANSWER HANDLER
# ==========================================

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
            "correct_q_indices": set(),
            "total_time": 0.0
        }

    p_data = participants[user_id]

    # Prevent double counting time / score if same question is answered twice
    if q_idx not in p_data["attempted_set"]:
        p_data["attempted_set"].add(q_idx)
        p_data["total_time"] += time_taken
        if "correct_q_indices" not in p_data:
            p_data["correct_q_indices"] = set()

        if user_selected == correct_option_id:
            p_data["correct"] += 1
            p_data["correct_q_indices"].add(q_idx)
        else:
            p_data["wrong"] += 1


# ==========================================
# FINAL LEADERBOARD GENERATION
# ==========================================

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
            text=f"🏁 Quiz Completed!\n\n📝 {quiz_name}\n\nNo participants attempted the quiz."
        )
        return

    # Sorting criteria:
    # 1. Score (Correct) descending
    # 2. Performance % descending
    # 3. Total Time ascending (faster is better)
    def sort_key(p):
        correct = p["correct"]
        attempted = len(p["attempted_set"])
        perf = (correct / attempted * 100.0) if attempted > 0 else 0.0
        total_time = p["total_time"]
        return (-correct, -perf, total_time)

    sorted_p = sorted(participants, key=sort_key)

    msg_lines = [
        "🏁 Quiz Completed!\n",
        f"📝 {quiz_name}\n",
        "🎯 Top Performers:\n"
    ]

    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

    for idx, p in enumerate(sorted_p, start=1):
        name = p["name"]
        correct = p["correct"]
        wrong = p["wrong"]
        attempted = len(p["attempted_set"])
        score = float(correct)
        time_str = format_time(p["total_time"])

        accuracy = (correct / total_q * 100.0) if total_q > 0 else 0.0
        performance = (correct / attempted * 100.0) if attempted > 0 else 0.0

        badge = rank_emojis.get(idx, f"{idx}.")

        line = (
            f"{badge} {name} | ✅ {correct} | ❌ {wrong} | 🎯 {score:.2f} | "
            f"⏱️ {time_str} | 📊 {accuracy:.1f}% | 🚀 {performance:.1f}%\n"
            f"────────────────"
        )
        msg_lines.append(line)

    full_text = "\n".join(msg_lines)

    # Handle message length limit (4096 chars)
    if len(full_text) <= 4000:
        await bot.send_message(chat_id=group_id, text=full_text)
    else:
        # Split into multiple chunks
        chunk = ""
        for line in msg_lines:
            if len(chunk) + len(line) + 2 > 4000:
                await bot.send_message(chat_id=group_id, text=chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await bot.send_message(chat_id=group_id, text=chunk)


from telegram import Bot

quiz_engine_bot = None

async def post_init(application):
    global quiz_engine_bot
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    request = HTTPXRequest(
        connection_pool_size=40,
        connect_timeout=8.0,
        read_timeout=8.0,
        write_timeout=8.0,
        pool_timeout=8.0,
        httpx_kwargs={"limits": limits}
    )
    quiz_engine_bot = Bot(token=config.BOT_TOKEN, request=request)
    await quiz_engine_bot.initialize()
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
        print("❌ ERROR: BOT_TOKEN is missing! Please set BOT_TOKEN in .env or environment variable.")
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

    # Log every incoming update for debugging
    app.add_handler(MessageHandler(filters.ALL, global_update_logger), group=-1)
    app.add_handler(CallbackQueryHandler(global_update_logger), group=-1)
    app.add_error_handler(error_handler)

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
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
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, chat_shared_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_private_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_private_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_private_message))

    print(f"🚀 Telegram Quiz Bot starting... (OWNER_ID={config.OWNER_ID}, GROUP_ID={config.GROUP_ID})", flush=True)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "poll_answer"]
    )


if __name__ == "__main__":
    main()
