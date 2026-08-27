"""
Telegram Quiz Bot — Main Application Module

A feature-rich Telegram bot for creating, managing, scheduling, and running
interactive quizzes in groups and private chats. Supports sections, timed
questions, live leaderboards, and quiz lifecycle controls (pause/resume/stop).
"""

import asyncio
import datetime
import html
import io
import logging
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Poll,
    Update,
)
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
import db
from parser import parse_questions_message

# ---------------------------------------------------------------------------
# Platform-specific UTF-8 Console Fix (Windows)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            new_mode = (mode.value & ~0x0040 & ~0x0020) | 0x0080
            kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception:
        pass

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", line_buffering=True
)

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress noisy HTTP client logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST_TZ = ZoneInfo("Asia/Kolkata")

# Timer bounds (seconds)
MIN_TIMER: int = 10
MAX_TIMER: int = 600
DEFAULT_TIMER: int = 15
DEFAULT_SPEED_DELTA: int = 5

# Telegram API limits
MAX_POLL_QUESTION_LENGTH: int = 200
MAX_POLL_OPTION_LENGTH: int = 40

# Network / connection pool settings
MAX_KEEPALIVE_CONNECTIONS: int = 20
MAX_CONNECTIONS: int = 40
TIMEOUT_SECONDS: float = 8.0
CONNECTION_POOL_SIZE: int = 40

# Retry settings
MAX_POLL_SEND_RETRIES: int = 3

# Scheduler polling interval (seconds)
SCHEDULER_POLL_INTERVAL: int = 10

# Option labels used in long-question display
OPTION_LABELS: Tuple[str, ...] = ("A", "B", "C", "D")

# ---------------------------------------------------------------------------
# In-Memory State
# ---------------------------------------------------------------------------

# Private-chat wizard state: user_id → { step, name, questions, … }
user_states: Dict[int, Dict[str, Any]] = {}

# Active quiz sessions: quiz_id → session dict
active_quizzes: Dict[str, Dict[str, Any]] = {}

# Telegram poll_id → { quiz_id, q_idx, correct_option_id, poll_start_time }
poll_id_map: Dict[str, Dict[str, Any]] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Utility Helpers
# ═══════════════════════════════════════════════════════════════════════════


def format_duration(seconds: float) -> str:
    """Format a duration in seconds into a human-readable string (e.g. '2m 15s')."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes > 0 else f"{secs}s"


def truncate(text: str, max_length: int) -> str:
    """Truncate *text* to *max_length*, appending '…' if shortened."""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"


def format_quiz_as_txt(questions: List[Dict[str, Any]]) -> str:
    """Serialize a list of question dicts to the plain-text import/export format."""
    blocks: List[str] = []
    for q in questions:
        lines = [q["question_text"]]
        for idx, opt in enumerate(q["options"]):
            suffix = " ✅" if idx == q["correct_option_id"] else ""
            lines.append(f"{opt}{suffix}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_httpx_request() -> HTTPXRequest:
    """Create a configured ``HTTPXRequest`` instance for the Telegram bot."""
    limits = httpx.Limits(
        max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
        max_connections=MAX_CONNECTIONS,
    )
    return HTTPXRequest(
        connection_pool_size=CONNECTION_POOL_SIZE,
        connect_timeout=TIMEOUT_SECONDS,
        read_timeout=TIMEOUT_SECONDS,
        write_timeout=TIMEOUT_SECONDS,
        pool_timeout=TIMEOUT_SECONDS,
        httpx_kwargs={"limits": limits},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Global Middleware: Update Logger & Error Handler
# ═══════════════════════════════════════════════════════════════════════════


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming update for observability."""
    if update.poll_answer:
        logger.debug(
            "[UPDATE] PollAnswer user_id=%s poll_id=%s",
            update.poll_answer.user.id,
            update.poll_answer.poll_id,
        )
    elif update.message:
        logger.info(
            "[UPDATE] Message user_id=%s chat_id=%s text=%s",
            update.message.from_user.id,
            update.message.chat_id,
            update.message.text,
        )
    elif update.callback_query:
        logger.info(
            "[UPDATE] CallbackQuery user_id=%s data='%s'",
            update.callback_query.from_user.id,
            update.callback_query.data,
        )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — logs the exception with traceback."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ═══════════════════════════════════════════════════════════════════════════
# Quiz Lifecycle Commands — Pause / Resume / Stop / Speed
# ═══════════════════════════════════════════════════════════════════════════


def _find_owned_sessions(
    user_id: int, chat_id: int, chat_is_private: bool
) -> List[Dict[str, Any]]:
    """Return active quiz sessions that belong to *user_id* in the relevant chat."""
    results: List[Dict[str, Any]] = []
    for session in active_quizzes.values():
        target_chat = session.get("group_id")
        if not chat_is_private and chat_id != target_chat:
            continue
        if session.get("user_id") != user_id:
            continue
        results.append(session)
    return results


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause the running quiz owned by the sender."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz to pause.")
        return

    sessions = _find_owned_sessions(user.id, chat.id, chat.type == "private")
    if not sessions:
        await update.message.reply_text(
            "❌ No active quiz in this chat that belongs to you."
        )
        return

    for session in sessions:
        session["paused"] = True
        try:
            await context.bot.send_message(
                chat_id=session["group_id"], text="⏸️ Quiz paused!"
            )
        except Exception as exc:
            logger.error("Failed to send pause notice: %s", exc)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume a previously paused quiz."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz to resume.")
        return

    sessions = _find_owned_sessions(user.id, chat.id, chat.type == "private")
    if not sessions:
        await update.message.reply_text(
            "❌ No active quiz in this chat that belongs to you."
        )
        return

    for session in sessions:
        session["paused"] = False
        try:
            await context.bot.send_message(
                chat_id=session["group_id"], text="▶️ Quiz resumed!"
            )
        except Exception as exc:
            logger.error("Failed to send resume notice: %s", exc)


async def stop_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the running quiz early."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz to stop.")
        return

    sessions = _find_owned_sessions(user.id, chat.id, chat.type == "private")
    if not sessions:
        await update.message.reply_text(
            "❌ No active quiz in this chat that belongs to you."
        )
        return

    for session in sessions:
        session["stopped"] = True
        try:
            await context.bot.send_message(
                chat_id=session["group_id"], text="⏹️ Quiz stopped by the owner!"
            )
        except Exception as exc:
            logger.error("Failed to send stop notice: %s", exc)


async def _adjust_speed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    direction: int,
) -> None:
    """Adjust the per-question timer for the running quiz.

    Args:
        direction: +1 to slow down, -1 to speed up.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz to adjust speed.")
        return

    delta = DEFAULT_SPEED_DELTA
    if context.args:
        try:
            delta = int(context.args[0])
        except ValueError:
            delta = DEFAULT_SPEED_DELTA

    sessions = _find_owned_sessions(user.id, chat.id, chat.type == "private")
    if not sessions:
        await update.message.reply_text(
            "❌ No active quiz in this chat that belongs to you."
        )
        return

    for session in sessions:
        current = session.get("timer", DEFAULT_TIMER)
        new_timer = max(MIN_TIMER // 2, min(MAX_TIMER, current + direction * delta))
        session["timer"] = new_timer
        label = "⚡ Speed increased" if direction == -1 else "🐢 Speed decreased"
        try:
            await context.bot.send_message(
                chat_id=session["group_id"],
                text=f"{label}!\n⏱️ Timer is now **{new_timer}s** per question.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send speed notice: %s", exc)


async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Decrease the per-question timer (speed up)."""
    await _adjust_speed(update, context, direction=-1)


async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Increase the per-question timer (slow down)."""
    await _adjust_speed(update, context, direction=1)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling Commands
# ═══════════════════════════════════════════════════════════════════════════


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedule a quiz to trigger at a specified time (IST).

    Usage: ``/schedule <quiz_id> <HH:MM>``
    """
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/schedule <quiz_id> <HH:MM>`\n"
            "Example: `/schedule GGN9PV0Q9 09:00`",
            parse_mode="Markdown",
        )
        return

    quiz_id = args[0].strip()
    time_str = args[1].strip()

    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID `{quiz_id}` not found.")
        return
    if quiz_data.get("user_id") != user.id:
        await update.message.reply_text(
            "❌ Access denied. You can only schedule quizzes you created."
        )
        return

    # Parse time
    try:
        hour, minute = (int(p) for p in time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(
            "❌ Invalid time. Use 24-hour `HH:MM` format (e.g. `09:00` or `21:30`).",
            parse_mode="Markdown",
        )
        return

    now = datetime.datetime.now(IST_TZ)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled <= now:
        scheduled += datetime.timedelta(days=1)

    db.save_schedule(quiz_id, user.id, scheduled.timestamp(), time_str)

    delta = scheduled - now
    total_secs = int(delta.total_seconds())
    hours_left, remainder = divmod(total_secs, 3600)
    minutes_left = remainder // 60

    await update.message.reply_text(
        f"✅ **Quiz Scheduled!**\n\n"
        f"📝 *{quiz_data['name']}* (ID: `{quiz_id}`)\n"
        f"🕒 {scheduled.strftime('%I:%M %p, %d %b')}\n"
        f"⏱️ Starts in {hours_left}h {minutes_left}m",
        parse_mode="Markdown",
    )


async def schedules_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List all active scheduled quizzes for the current user."""
    user = update.effective_user
    if not user:
        return

    all_schedules = db.get_active_schedules()
    user_schedules = [s for s in all_schedules if s.get("user_id") == user.id]
    if not user_schedules:
        await update.message.reply_text("ℹ️ You have no active scheduled quizzes.")
        return

    lines = ["📅 **Your Active Schedules:**\n"]
    for s in user_schedules:
        dt = datetime.datetime.fromtimestamp(s["scheduled_timestamp"], tz=IST_TZ)
        lines.append(
            f"• ID: `{s['quiz_id']}` — {dt.strftime('%I:%M %p, %d %b')}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def unschedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove a scheduled quiz.

    Usage: ``/unschedule <quiz_id>``
    """
    user = update.effective_user
    if not user:
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: `/unschedule <quiz_id>`", parse_mode="Markdown")
        return

    quiz_id = context.args[0].strip()
    db.delete_schedule(quiz_id, user.id)
    await update.message.reply_text(
        f"✅ Schedule for quiz `{quiz_id}` has been removed.", parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Background Scheduler Loop
# ═══════════════════════════════════════════════════════════════════════════


async def _scheduler_loop(application) -> None:
    """Periodically check for due scheduled quizzes and notify the owner."""
    logger.info("⏰ Background scheduler loop started.")
    try:
        while True:
            try:
                now_ts = time.time()
                schedules = await asyncio.to_thread(db.get_active_schedules)
                for s in schedules:
                    if now_ts >= s["scheduled_timestamp"]:
                        quiz_id = s["quiz_id"]
                        user_id = s.get("user_id")
                        logger.info("⏰ Triggering scheduled quiz: %s", quiz_id)
                        await asyncio.to_thread(db.delete_schedule, quiz_id)
                        quiz_data = await asyncio.to_thread(db.get_quiz, quiz_id)
                        if quiz_data and user_id:
                            try:
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=(
                                        f"⏰ Scheduled time reached for quiz "
                                        f"*{quiz_data['name']}* (`{quiz_id}`)!"
                                    ),
                                    parse_mode="Markdown",
                                )
                            except Exception as exc:
                                logger.error(
                                    "Failed to notify schedule trigger: %s", exc
                                )
            except Exception as exc:
                logger.error("Exception in scheduler loop: %s", exc)
            await asyncio.sleep(SCHEDULER_POLL_INTERVAL)
    except asyncio.CancelledError:
        logger.info("⏰ Scheduler loop stopped.")


# ═══════════════════════════════════════════════════════════════════════════
# Core Bot Commands
# ═══════════════════════════════════════════════════════════════════════════


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — initiate quiz creation or launch a quiz via deep link."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    # Deep-link: /start quiz_<ID>
    args = context.args or []
    if args and args[0].startswith("quiz_"):
        quiz_id = args[0].removeprefix("quiz_").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await update.message.reply_text("❌ Quiz not found!")
            return

        if chat.type != "private":
            logger.info("Launching quiz %s in group %s", quiz_id, chat.id)
            await update.message.reply_text(
                f"🚀 Quiz *{quiz_data['name']}* starting in this chat!",
                parse_mode="Markdown",
            )
            asyncio.create_task(
                run_quiz_session(context.bot, chat.id, quiz_data, update.message)
            )
            return

        await _send_quiz_created_screen(update, context, quiz_data)
        return

    # Group chat — prompt user to DM
    if chat.type != "private":
        await update.message.reply_text(
            "👋 Hello! Send /start in a private chat with me to create quizzes!"
        )
        return

    # Private chat — start the quiz-creation wizard
    user_states[user.id] = {"step": "WAITING_NAME", "name": "", "questions": []}
    await update.message.reply_text(
        "🎯 **Welcome to Quiz Bot!**\n\n"
        "Create your own quiz for free.\n\n"
        "📝 Please send the **quiz name**:",
        parse_mode="Markdown",
    )


async def myquizzes_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List all quizzes created by the current user."""
    user = update.effective_user
    if not user:
        return

    quizzes = db.get_user_quizzes(user.id)
    if not quizzes:
        await update.message.reply_text(
            "ℹ️ You haven't created any quizzes yet.\n"
            "Send /start to create your first quiz!"
        )
        return

    lines = [f"📚 **Your Quizzes** (Total: {len(quizzes)})\n"]
    keyboard: List[List[InlineKeyboardButton]] = []

    for q in quizzes:
        qid = q["quiz_id"]
        lines.append(
            f"• **{q['name']}** — {len(q.get('questions', []))} Qs — ID: `{qid}`"
        )
        keyboard.append(
            [
                InlineKeyboardButton(f"🚀 Start {qid}", callback_data=f"start_p_{qid}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"ed_mgr_{qid}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"del_confirm_{qid}"),
            ]
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any active creation or editing wizard."""
    user = update.effective_user
    if not user:
        return

    if user.id not in user_states:
        await update.message.reply_text("ℹ️ No active process to cancel.")
        return

    del user_states[user.id]
    await update.message.reply_text(
        "🚫 Process cancelled. Send /start to begin a new quiz."
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Finish adding questions and move to the timer-setting step."""
    user = update.effective_user
    if not user:
        return

    state = user_states.get(user.id)
    if not state or state.get("step") not in ("WAITING_QUESTIONS", "WAITING_TIMER"):
        await update.message.reply_text(
            "❌ No active quiz creation session. Send /start to create a quiz."
        )
        return

    questions = state.get("questions", [])
    if len(questions) < 1:
        await update.message.reply_text(
            "❌ At least 1 question is required. Add questions or send /cancel."
        )
        return

    state["step"] = "WAITING_TIMER"
    await update.message.reply_text(
        f"⏳ Enter the per-question timer in seconds (minimum {MIN_TIMER}):"
    )


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the quiz editor for a given quiz ID.

    Usage: ``/edit <quiz_id>``
    """
    user = update.effective_user
    if not user:
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: `/edit <quiz_id>`", parse_mode="Markdown")
        return

    quiz_id = context.args[0].strip()
    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz `{quiz_id}` not found.", parse_mode="Markdown")
        return
    if quiz_data.get("user_id") != user.id:
        await update.message.reply_text(
            "❌ Access denied. You can only edit quizzes you created."
        )
        return

    await _send_quiz_editor_screen(update, context, quiz_data)


async def done_edit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Finish adding questions during an edit session."""
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
        await update.message.reply_text("✅ Questions updated successfully!")
        await _send_quiz_editor_screen(update, context, quiz_data)


# ═══════════════════════════════════════════════════════════════════════════
# Quiz Creation / Editing Wizard — Private Messages
# ═══════════════════════════════════════════════════════════════════════════


async def handle_private_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle .txt file uploads during quiz creation or editing."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text(
            "Send /start to create a new quiz or /myquizzes to view yours."
        )
        return

    step = state.get("step")
    if step not in ("WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"):
        await update.message.reply_text("❌ A file upload is not expected at this step.")
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ Please upload a valid `.txt` file.", parse_mode="Markdown")
        return

    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        file_bytes = await telegram_file.download_as_bytearray()

        # Decode with BOM-aware fallback
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text_content = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text_content = file_bytes.decode("latin-1", errors="ignore")

        parsed, errors = parse_questions_message(text_content)

        if errors:
            preview = "\n".join(errors[:10])
            suffix = (
                f"\n…and {len(errors) - 10} more errors."
                if len(errors) > 10
                else ""
            )
            await update.message.reply_text(
                f"⚠️ **Format errors found:**\n\n{preview}{suffix}",
                parse_mode="Markdown",
            )

        if not parsed:
            await update.message.reply_text(
                "❌ No valid questions found. Each question must have exactly "
                "4 options with 1 marked correct (✅)."
            )
            return

        if step == "WAITING_QUESTIONS":
            questions = state.setdefault("questions", [])
            questions.extend(parsed)
            await update.message.reply_text(
                f"✅ **{len(parsed)} question(s) added!** Total: {len(questions)}\n\n"
                "Send more questions or type /done when finished.",
                parse_mode="Markdown",
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data and quiz_data.get("user_id") == user.id:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, user.id, questions)
                await update.message.reply_text(
                    f"✅ {len(parsed)} question(s) added! Total: {len(questions)}.\n"
                    "Send more or type /done_edit when finished.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("❌ Quiz not found or access denied.")
    except Exception as exc:
        logger.exception("Failed to process uploaded document: %s", exc)
        await update.message.reply_text(f"❌ Failed to process file: {exc}")


async def handle_private_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route free-text messages through the quiz-creation wizard steps."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text(
            "Send /start to create a new quiz, or /myquizzes to view your quizzes."
        )
        return

    text = (update.message.text or "").strip()
    step = state.get("step")

    # ── Step: Quiz Name ──────────────────────────────────────────────────
    if step == "WAITING_NAME":
        if not text:
            await update.message.reply_text("Please send a valid quiz name.")
            return
        state["name"] = text
        state["step"] = "WAITING_QUESTIONS"
        await update.message.reply_text(
            f"✅ **Quiz name saved:** {text}\n\n"
            "Now send your questions. You can either:\n"
            "1. Send them as text in this format:\n\n"
            "```\n"
            "Question text\n"
            "Option 1\n"
            "Option 2 ✅\n"
            "Option 3\n"
            "Option 4\n"
            "```\n\n"
            "2. Or upload a `.txt` file.\n\n"
            "When done, send /done. To cancel, send /cancel.",
            parse_mode="Markdown",
        )
        return

    # ── Step: Add Questions ──────────────────────────────────────────────
    if step == "WAITING_QUESTIONS":
        if not text:
            return
        parsed, errors = parse_questions_message(text)
        if errors:
            await update.message.reply_text(
                "⚠️ **Format issues:**\n\n" + "\n".join(errors[:5]),
                parse_mode="Markdown",
            )
        if not parsed:
            await update.message.reply_text(
                "❌ Invalid format. Each question needs 4 options with "
                "1 correct answer marked ✅."
            )
            return
        questions = state.setdefault("questions", [])
        questions.extend(parsed)
        await update.message.reply_text(
            f"✅ **{len(parsed)} question(s) saved!** Total: {len(questions)}\n"
            "Send more questions or type /done.",
            parse_mode="Markdown",
        )
        return

    # ── Step: Timer ──────────────────────────────────────────────────────
    if step == "WAITING_TIMER":
        try:
            timer_val = int(text)
            if timer_val < MIN_TIMER:
                await update.message.reply_text(
                    f"⏳ Minimum timer is {MIN_TIMER} seconds."
                )
                return
        except ValueError:
            await update.message.reply_text("⏳ Please enter a valid number (e.g. 15).")
            return

        state["timer"] = timer_val
        state["step"] = "WAITING_SEC_CHOICE"
        keyboard = [
            [
                InlineKeyboardButton("🟢 Yes", callback_data="create_sec_yes"),
                InlineKeyboardButton("⚪ No", callback_data="create_sec_no"),
            ]
        ]
        await update.message.reply_text(
            f"Timer set to **{timer_val}s**.\n\n"
            "📚 Would you like to divide this quiz into **sections**?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # ── Step: Section Count ──────────────────────────────────────────────
    if step == "CREATE_SEC_COUNT":
        try:
            sec_count = int(text)
            if sec_count < 1:
                await update.message.reply_text("❌ At least 1 section is required.")
                return
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number (e.g. 2).")
            return

        total_q = len(state.get("questions", []))
        if sec_count > total_q:
            await update.message.reply_text(
                f"❌ Section count ({sec_count}) cannot exceed total questions ({total_q})."
            )
            return

        state["sec_total_count"] = sec_count
        state["sec_current_idx"] = 0
        state["temp_sections"] = []
        state["step"] = "CREATE_SEC_NAME"
        await update.message.reply_text("📌 Enter the name for **Section 1**:", parse_mode="Markdown")
        return

    # ── Step: Section Name ───────────────────────────────────────────────
    if step == "CREATE_SEC_NAME":
        if not text:
            await update.message.reply_text("Please enter a valid section name.")
            return
        state["curr_sec_name"] = text
        state["step"] = "CREATE_SEC_RANGE"
        idx = state.get("sec_current_idx", 0) + 1
        await update.message.reply_text(
            f"🔢 Enter the question range for Section {idx} (e.g. `1-10`):",
            parse_mode="Markdown",
        )
        return

    # ── Step: Section Range ──────────────────────────────────────────────
    if step == "CREATE_SEC_RANGE":
        total_q = len(state.get("questions", []))
        nums = re.findall(r"\d+", text.replace("to", "-").replace("TO", "-"))
        if len(nums) != 2:
            await update.message.reply_text(
                "❌ Invalid format. Enter a range like `1-10`.", parse_mode="Markdown"
            )
            return

        start_q, end_q = int(nums[0]), int(nums[1])
        if start_q < 1 or end_q > total_q or start_q > end_q:
            await update.message.reply_text(
                f"❌ Range must be between 1 and {total_q}. Please try again."
            )
            return

        temp_sections = state.get("temp_sections", [])
        for s in temp_sections:
            if max(s["start"], start_q) <= min(s["end"], end_q):
                await update.message.reply_text(
                    f"❌ Overlaps with *{s['name']}* ({s['start']}-{s['end']}). Try again.",
                    parse_mode="Markdown",
                )
                return

        temp_sections.append(
            {"name": state.get("curr_sec_name", "Section"), "start": start_q, "end": end_q}
        )
        state["temp_sections"] = temp_sections
        state["sec_current_idx"] += 1

        if state["sec_current_idx"] < state["sec_total_count"]:
            state["step"] = "CREATE_SEC_NAME"
            await update.message.reply_text(
                f"📌 Enter the name for **Section {state['sec_current_idx'] + 1}**:",
                parse_mode="Markdown",
            )
            return

        # All sections defined — finalize quiz
        creator_name = _full_name(user)
        quiz_id = db.save_quiz(
            user.id,
            state.get("name"),
            state.get("timer", DEFAULT_TIMER),
            state.get("questions", []),
            creator_name=creator_name,
            sections_enabled=1,
            sections=temp_sections,
        )
        del user_states[user.id]
        quiz_data = db.get_quiz(quiz_id)
        await _send_quiz_created_screen(update, context, quiz_data)
        return

    # ── Step: Edit Name ──────────────────────────────────────────────────
    if step == "EDIT_NAME":
        quiz_id = state.get("quiz_id")
        if not text:
            await update.message.reply_text("Please send a valid quiz name.")
            return
        db.update_quiz_name(quiz_id, user.id, text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Quiz name updated to `{text}`!", parse_mode="Markdown")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await _send_quiz_editor_screen(update, context, quiz_data)
        return

    # ── Step: Edit Timer ─────────────────────────────────────────────────
    if step == "EDIT_TIMER":
        quiz_id = state.get("quiz_id")
        try:
            timer_val = int(text)
            if timer_val < MIN_TIMER:
                await update.message.reply_text(
                    f"⏳ Minimum timer is {MIN_TIMER} seconds."
                )
                return
        except ValueError:
            await update.message.reply_text("⏳ Please enter a valid number.")
            return
        db.update_quiz_timer(quiz_id, user.id, timer_val)
        del user_states[user.id]
        await update.message.reply_text(
            f"✅ Timer updated to `{timer_val}s`!", parse_mode="Markdown"
        )
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await _send_quiz_editor_screen(update, context, quiz_data)
        return

    # ── Step: Edit — Add More Questions ──────────────────────────────────
    if step == "EDIT_ADD_QUESTIONS":
        quiz_id = state.get("quiz_id")
        parsed, errors = parse_questions_message(text)
        if errors:
            await update.message.reply_text("\n".join(errors[:5]))
        if not parsed:
            await update.message.reply_text("❌ Invalid question format.")
            return
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data and quiz_data.get("user_id") == user.id:
            questions = quiz_data.get("questions", [])
            questions.extend(parsed)
            db.update_quiz_questions(quiz_id, user.id, questions)
            await update.message.reply_text(
                f"✅ {len(parsed)} question(s) added! Total: {len(questions)}.\n"
                "Send more or type /done_edit when finished."
            )
        return


# ═══════════════════════════════════════════════════════════════════════════
# Inline Keyboard Callback Router
# ═══════════════════════════════════════════════════════════════════════════


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Central dispatcher for all inline-keyboard callback queries."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user
    data = query.data

    # ── Section choice: No ───────────────────────────────────────────────
    if data == "create_sec_no":
        state = user_states.get(user.id)
        if not state:
            return
        creator_name = _full_name(user)
        quiz_id = db.save_quiz(
            user.id,
            state.get("name"),
            state.get("timer", DEFAULT_TIMER),
            state.get("questions", []),
            creator_name=creator_name,
            sections_enabled=0,
            sections=[],
        )
        del user_states[user.id]
        quiz_data = db.get_quiz(quiz_id)
        await _send_quiz_created_screen(update, context, quiz_data)
        return

    # ── Section choice: Yes ──────────────────────────────────────────────
    if data == "create_sec_yes":
        state = user_states.get(user.id)
        if not state:
            return
        state["step"] = "CREATE_SEC_COUNT"
        await query.message.reply_text(
            "📚 How many sections would you like? (e.g. 2):"
        )
        return

    # ── Quick-start quiz from /myquizzes ─────────────────────────────────
    if data.startswith("start_p_"):
        quiz_id = data.removeprefix("start_p_").strip()
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await query.message.reply_text(
                f"🚀 Quiz *{quiz_data['name']}* starting…", parse_mode="Markdown"
            )
            asyncio.create_task(
                run_quiz_session(
                    context.bot, query.message.chat_id, quiz_data, query.message
                )
            )
        return

    # ── Open editor from /myquizzes ──────────────────────────────────────
    if data.startswith("ed_mgr_"):
        quiz_id = data.removeprefix("ed_mgr_").strip()
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            if quiz_data.get("user_id") != user.id:
                await query.message.reply_text(
                    "❌ Access denied. Only the quiz owner can edit."
                )
                return
            await _send_quiz_editor_screen(update, context, quiz_data)
        return

    # ── Delete quiz ──────────────────────────────────────────────────────
    if data.startswith("del_confirm_"):
        quiz_id = data.removeprefix("del_confirm_").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data or quiz_data.get("user_id") != user.id:
            await query.message.reply_text("❌ Access denied or quiz not found.")
            return
        db.delete_quiz(quiz_id, user.id)
        await query.message.reply_text(
            f"🗑️ Quiz `{quiz_id}` deleted.", parse_mode="Markdown"
        )
        return

    # ── Share link ───────────────────────────────────────────────────────
    if data.startswith("share_"):
        quiz_id = data.removeprefix("share_")
        bot_username = (await context.bot.get_me()).username
        await query.message.reply_text(
            f"🔗 **Share Link:**\nhttps://t.me/{bot_username}?start=quiz_{quiz_id}",
            parse_mode="Markdown",
        )
        return

    # ── Export quiz as .txt ──────────────────────────────────────────────
    if data.startswith("ed_exp_"):
        quiz_id = data.removeprefix("ed_exp_")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data or quiz_data.get("user_id") != user.id:
            await query.message.reply_text("❌ Quiz not found or access denied.")
            return
        questions = quiz_data.get("questions", [])
        file_buf = io.BytesIO(format_quiz_as_txt(questions).encode("utf-8"))
        file_buf.name = f"quiz_{quiz_id}.txt"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_buf,
            caption=f"📤 Exported quiz `{quiz_id}` — {len(questions)} question(s)",
        )
        return

    # ── Editor actions ───────────────────────────────────────────────────
    if data.startswith("ed_name_"):
        quiz_id = data.removeprefix("ed_name_")
        user_states[user.id] = {"step": "EDIT_NAME", "quiz_id": quiz_id}
        await query.message.reply_text(
            f"✏️ Send the new name for quiz `{quiz_id}`:", parse_mode="Markdown"
        )
        return

    if data.startswith("ed_timer_"):
        quiz_id = data.removeprefix("ed_timer_")
        user_states[user.id] = {"step": "EDIT_TIMER", "quiz_id": quiz_id}
        await query.message.reply_text(
            f"⏱️ Send the new timer (seconds) for quiz `{quiz_id}`:",
            parse_mode="Markdown",
        )
        return

    if data.startswith("ed_addq_"):
        quiz_id = data.removeprefix("ed_addq_")
        user_states[user.id] = {"step": "EDIT_ADD_QUESTIONS", "quiz_id": quiz_id}
        await query.message.reply_text(
            f"➕ Send additional questions for quiz `{quiz_id}`.\n"
            "Type /done_edit when finished.",
            parse_mode="Markdown",
        )
        return

    if data == "ed_close":
        try:
            await query.message.delete()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Quiz Execution Engine
# ═══════════════════════════════════════════════════════════════════════════


def _full_name(user) -> str:
    """Build a display name from a Telegram User object."""
    return user.first_name + (f" {user.last_name}" if user.last_name else "")


def _cleanup_session(quiz_id: str) -> None:
    """Remove a quiz session and its poll mappings from memory."""
    active_quizzes.pop(quiz_id, None)
    stale = [pid for pid, info in poll_id_map.items() if info.get("quiz_id") == quiz_id]
    for pid in stale:
        poll_id_map.pop(pid, None)


async def run_quiz_session(
    bot, group_id: int, quiz_data: dict, status_msg=None
) -> None:
    """Run a complete quiz session, sending polls one-by-one and collecting answers."""
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    user_id = quiz_data.get("user_id")

    # Prevent duplicate launches
    if quiz_id in active_quizzes:
        existing = active_quizzes[quiz_id]
        if existing.get("active") and not existing.get("stopped"):
            if status_msg:
                try:
                    await status_msg.reply_text(
                        f"⚠️ Quiz *{name}* is already running!",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            return

    timer = quiz_data["timer"]
    questions = quiz_data["questions"]
    total_q = len(questions)
    sections = sorted(quiz_data.get("sections", []), key=lambda s: s["start"])
    sec_enabled = quiz_data.get("sections_enabled", 0)

    session: Dict[str, Any] = {
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
        "leaderboard_sent": False,
    }
    active_quizzes[quiz_id] = session

    try:
        # Announcement
        try:
            await bot.send_message(
                chat_id=group_id,
                text=(
                    f"🚀 **Quiz Starting!**\n\n"
                    f"📜 **Name:** {name}\n"
                    f"🔢 **Questions:** {total_q}\n"
                    f"⏳ **Timer:** {timer}s per question"
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send quiz announcement: %s", exc)

        idx = 1
        while idx <= total_q:
            # Check stop
            if session.get("stopped"):
                break

            # Handle pause
            while session.get("paused"):
                if session.get("stopped"):
                    break
                await asyncio.sleep(0.5)
            if session.get("stopped"):
                break

            try:
                q_item = questions[idx - 1]
                raw_question = q_item["question_text"]
                options = q_item["options"]
                correct_id = q_item["correct_option_id"]

                # Section header
                if sec_enabled and sections:
                    for sec in sections:
                        if sec["start"] == idx:
                            try:
                                await bot.send_message(
                                    chat_id=group_id,
                                    text=(
                                        "━━━━━━━━━━━━━━━━━━\n"
                                        f"📚 **SECTION: {sec['name']}**\n"
                                        f"Questions {sec['start']}–{sec['end']}\n"
                                        "━━━━━━━━━━━━━━━━━━"
                                    ),
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass

                # Pre-send long question text if it exceeds poll limits
                q_text = f"[{idx}/{total_q}] {raw_question}"
                has_long_option = any(len(o) > MAX_POLL_OPTION_LENGTH for o in options)
                is_long_question = len(q_text) > MAX_POLL_QUESTION_LENGTH

                if is_long_question or has_long_option:
                    formatted_opts = "\n".join(
                        f"  {OPTION_LABELS[i]}. {opt}" for i, opt in enumerate(options)
                    )
                    try:
                        await bot.send_message(
                            chat_id=group_id,
                            text=f"📋 **Q{idx}/{total_q}** ❓ {raw_question}\n\n{formatted_opts}",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)

                # Send the quiz poll
                current_timer = session.get("timer", timer)
                open_period = max(5, min(int(current_timer), MAX_TIMER))
                poll_question = truncate(q_text, MAX_POLL_QUESTION_LENGTH)
                poll_options = [truncate(o, MAX_POLL_OPTION_LENGTH) for o in options]

                poll_msg = None
                for attempt in range(1, MAX_POLL_SEND_RETRIES + 1):
                    try:
                        poll_msg = await bot.send_poll(
                            chat_id=group_id,
                            question=poll_question,
                            options=poll_options,
                            type=Poll.QUIZ,
                            correct_option_id=correct_id,
                            is_anonymous=False,
                            open_period=open_period,
                        )
                        break
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after))
                    except Exception as exc:
                        logger.error("Error sending poll Q%d (attempt %d): %s", idx, attempt, exc)
                        await asyncio.sleep(1.0)

                if not poll_msg:
                    idx += 1
                    continue

                poll_created = time.monotonic()
                poll_wall_time = time.time()

                poll_id_map[poll_msg.poll.id] = {
                    "quiz_id": quiz_id,
                    "q_idx": idx,
                    "correct_option_id": correct_id,
                    "poll_start_time": poll_wall_time,
                }

                # Wait for the timer to expire
                target_end = poll_created + current_timer
                while time.monotonic() < target_end:
                    if session.get("stopped"):
                        break
                    await asyncio.sleep(0.5)

                idx += 1

            except Exception as exc:
                logger.error("Exception processing Q%d: %s", idx, exc)
                idx += 1

        # Leaderboard
        await _send_leaderboard(bot, group_id, session)

    except Exception as exc:
        logger.error("Fatal exception in quiz session: %s", exc)
    finally:
        _cleanup_session(quiz_id)


async def handle_poll_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record a participant's poll answer and update their score."""
    answer = update.poll_answer
    if not answer:
        return

    poll_info = poll_id_map.get(answer.poll_id)
    if not poll_info:
        return

    session = active_quizzes.get(poll_info["quiz_id"])
    if not session or session.get("stopped") or session.get("leaderboard_sent"):
        return

    if not answer.option_ids:
        return

    user = answer.user
    selected = answer.option_ids[0]
    q_idx = poll_info["q_idx"]
    correct = poll_info["correct_option_id"]
    time_taken = max(0.0, time.time() - poll_info["poll_start_time"])

    participants = session["participants"]
    if user.id not in participants:
        participants[user.id] = {
            "user_id": user.id,
            "name": _full_name(user),
            "username": user.username or "",
            "correct": 0,
            "wrong": 0,
            "attempted_set": set(),
            "total_time": 0.0,
        }

    entry = participants[user.id]
    if q_idx not in entry["attempted_set"]:
        entry["attempted_set"].add(q_idx)
        entry["total_time"] += time_taken
        if selected == correct:
            entry["correct"] += 1
        else:
            entry["wrong"] += 1


# ═══════════════════════════════════════════════════════════════════════════
# Leaderboard & UI Screens
# ═══════════════════════════════════════════════════════════════════════════

RANK_BADGES = {1: "🥇", 2: "🥈", 3: "🥉"}


async def _send_leaderboard(bot, group_id: int, session: dict) -> None:
    """Compile and send the final leaderboard to the group chat."""
    if session.get("leaderboard_sent"):
        return
    session["leaderboard_sent"] = True

    participants = list(session["participants"].values())
    total_q = session["total_questions"]
    quiz_name = session.get("name", "Quiz")

    if not participants:
        await bot.send_message(
            chat_id=group_id,
            text=(
                f"🏁 **Quiz Complete!**\n\n"
                f"📝 **{quiz_name}**\n\n"
                "No participants recorded."
            ),
            parse_mode="Markdown",
        )
        return

    def sort_key(p):
        attempted = len(p["attempted_set"])
        accuracy = (p["correct"] / attempted * 100) if attempted else 0
        return (-p["correct"], -accuracy, p["total_time"])

    sorted_participants = sorted(participants, key=sort_key)

    lines = [
        "🏁 **Quiz Complete!**\n",
        f"📝 **{quiz_name}**\n",
        "🎯 **Leaderboard:**\n",
    ]

    for rank, p in enumerate(sorted_participants, start=1):
        badge = RANK_BADGES.get(rank, f"{rank}.")
        accuracy = (p["correct"] / total_q * 100) if total_q else 0
        lines.append(
            f"{badge} **{p['name']}** — "
            f"✅ {p['correct']} ❌ {p['wrong']} "
            f"⏱️ {format_duration(p['total_time'])} "
            f"📊 {accuracy:.1f}%"
        )

    await bot.send_message(
        chat_id=group_id, text="\n".join(lines), parse_mode="Markdown"
    )


async def _send_quiz_created_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict
) -> None:
    """Display the 'Quiz Created' confirmation with action buttons."""
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    creator = quiz_data.get("creator_name", "Unknown")

    bot_username = await _get_bot_username(context.bot)
    base_url = f"https://t.me/{bot_username}" if bot_username else "https://t.me/"

    msg = (
        f"🎉 **Quiz Created!**\n\n"
        f"📝 **Name:** {html.escape(name)}\n"
        f"#️⃣ **Questions:** {q_count}\n"
        f"⏰ **Timer:** {timer}s\n"
        f"🆔 **ID:** <code>{html.escape(quiz_id)}</code>\n"
        f"👤 **Creator:** {html.escape(creator)}"
    )

    keyboard = [
        [InlineKeyboardButton("🎯 Start in Private Chat", url=f"{base_url}?start=quiz_{quiz_id}")],
        [InlineKeyboardButton("🚀 Add & Run in Group", url=f"{base_url}?startgroup=quiz_{quiz_id}")],
        [InlineKeyboardButton("🔗 Share Link", callback_data=f"share_{quiz_id}")],
    ]

    target = update.callback_query.message if update.callback_query else update.message
    try:
        await target.reply_text(
            msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception:
        await target.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


async def _send_quiz_editor_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict
) -> None:
    """Display the quiz editor panel with edit actions."""
    quiz_id = quiz_data["quiz_id"]
    msg = (
        f"🎯 **Quiz Editor**\n\n"
        f"📌 **Name:** {quiz_data['name']}\n"
        f"🔢 **Questions:** {len(quiz_data['questions'])}\n"
        f"⏱️ **Timer:** {quiz_data['timer']}s\n"
        f"🆔 **ID:** `{quiz_id}`"
    )
    keyboard = [
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"ed_name_{quiz_id}"),
            InlineKeyboardButton("⏱️ Timer", callback_data=f"ed_timer_{quiz_id}"),
        ],
        [
            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
            InlineKeyboardButton("📤 Export TXT", callback_data=f"ed_exp_{quiz_id}"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="ed_close")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                msg, reply_markup=markup, parse_mode="Markdown"
            )
        except Exception:
            await update.callback_query.message.reply_text(
                msg, reply_markup=markup, parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(msg, reply_markup=markup, parse_mode="Markdown")


async def _get_bot_username(bot) -> str:
    """Retrieve the bot's username, caching the result."""
    if getattr(bot, "username", None):
        return bot.username
    try:
        me = await bot.get_me()
        return me.username
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Application Lifecycle Hooks
# ═══════════════════════════════════════════════════════════════════════════


async def post_init(application) -> None:
    """Called after the application is initialized — starts the scheduler."""
    task = asyncio.create_task(_scheduler_loop(application))
    application.bot_data["scheduler_task"] = task


async def post_shutdown(application) -> None:
    """Called during shutdown — cancels the scheduler loop."""
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Initialize the database, build the Telegram application, and start polling."""
    db.init_db()

    token = config.BOT_TOKEN
    if not token:
        logger.critical("BOT_TOKEN is missing! Set it in your .env file.")
        return

    app = (
        ApplicationBuilder()
        .token(token)
        .request(build_httpx_request())
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── Middleware (group -1 runs before regular handlers) ────────────────
    app.add_handler(MessageHandler(filters.ALL, log_update), group=-1)
    app.add_handler(CallbackQueryHandler(log_update), group=-1)
    app.add_error_handler(handle_error)

    # ── Command Handlers ─────────────────────────────────────────────────
    commands = {
        "start": start_command,
        "myquizzes": myquizzes_command,
        "cancel": cancel_command,
        "done": done_command,
        "schedule": schedule_command,
        "schedules": schedules_command,
        "unschedule": unschedule_command,
        "pause": pause_command,
        "resume": resume_command,
        "stop": stop_quiz_command,
        "fast": fast_command,
        "slow": slow_command,
        "edit": edit_command,
        "done_edit": done_edit_command,
    }
    for cmd, handler_fn in commands.items():
        app.add_handler(CommandHandler(cmd, handler_fn))

    # ── Other Handlers ───────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_private_document))
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_private_message)
    )

    logger.info("🚀 Quiz Bot starting…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "poll_answer"],
    )


if __name__ == "__main__":
    main()
