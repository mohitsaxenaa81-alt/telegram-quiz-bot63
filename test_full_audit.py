import asyncio
import time
import os
import db
import config
from parser import parse_questions_message
import quiz_bot
from unittest.mock import AsyncMock, MagicMock

async def full_system_audit():
    print("=" * 60)
    print("🔍 RUNNING FULL END-TO-END DEEP SYSTEM AUDIT")
    print("=" * 60)

    # 1. DB Audit
    print("1️⃣ Testing Database Operations...")
    db.init_db()
    test_q_list = [
        {"question_text": "Sample Q1?", "options": ["Opt1", "Opt2"], "correct_option_id": 0},
        {"question_text": "Sample Q2?", "options": ["Opt1", "Opt2"], "correct_option_id": 1}
    ]
    q_id = db.save_quiz("Audit Quiz", 15, test_q_list, "Tester", 1, [{"name": "Sec1", "start": 1, "end": 2}])
    assert q_id is not None, "DB save failed!"
    
    fetched = db.get_quiz(q_id)
    assert fetched is not None and fetched["name"] == "Audit Quiz", "DB fetch failed!"
    assert len(fetched["questions"]) == 2, "Questions count mismatch!"
    assert fetched["sections_enabled"] == 1, "Sections status mismatch!"
    print("   ✅ Database Save & Fetch: PASSED")

    # 2. Parser Audit
    print("2️⃣ Testing Bilingual Question Parser...")
    raw_text = """1. Which element has atomic number 1?
(A) Hydrogen ✅
(B) Helium
(C) Lithium
(D) Beryllium"""
    parsed = parse_questions_message(raw_text)
    assert len(parsed) == 1, "Parser failed to parse single question!"
    assert parsed[0]["correct_option_id"] == 0, "Parser failed to detect correct option!"
    print("   ✅ Bilingual Question Parser: PASSED")

    # 3. Quiz Execution Engine & Controls Audit
    print("3️⃣ Testing Quiz Session Execution, Controls & Leaderboard...")
    mock_bot = MagicMock()
    mock_bot.send_poll = AsyncMock(return_value=MagicMock(message_id=999, date=MagicMock(timestamp=lambda: time.time()), poll=MagicMock(id="p999")))
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=888))
    mock_bot.stop_poll = AsyncMock(return_value=MagicMock())

    session_quiz_data = {
        "quiz_id": "AUDIT_EXEC_123",
        "name": "Audit Execution Quiz",
        "timer": 0.2, # Fast test timer
        "questions": test_q_list,
        "sections_enabled": 0,
        "sections": []
    }

    # Test quiz launch
    exec_task = asyncio.create_task(quiz_bot.run_quiz_session(mock_bot, -100999, session_quiz_data))
    await asyncio.sleep(0.1)

    # Test active session presence
    assert "AUDIT_EXEC_123" in quiz_bot.active_quizzes, "Active quiz not tracked!"
    active_sess = quiz_bot.active_quizzes["AUDIT_EXEC_123"]

    # Test PollAnswer registration
    mock_ans_update = MagicMock()
    mock_ans_update.poll_answer.poll_id = "p999"
    mock_ans_update.poll_answer.user.id = 111
    mock_ans_update.poll_answer.user.first_name = "Rahul"
    mock_ans_update.poll_answer.user.last_name = ""
    mock_ans_update.poll_answer.user.username = "rahul123"
    mock_ans_update.poll_answer.option_ids = [0]

    await quiz_bot.handle_poll_answer(mock_ans_update, None)
    assert 111 in active_sess["participants"], "Participant not registered on PollAnswer!"
    assert active_sess["participants"][111]["correct"] == 1, "Score calculation error!"

    await exec_task
    assert active_sess.get("leaderboard_sent", False) == True, "Leaderboard was not sent!"
    assert "AUDIT_EXEC_123" not in quiz_bot.active_quizzes, "Cleanup failed after quiz completion!"
    print("   ✅ Execution Engine, Score Tracking & Cleanup: PASSED")

    print("=" * 60)
    print("🎉 ALL SYSTEM AUDIT CHECKS PASSED WITH 100% SUCCESS!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(full_system_audit())
