import asyncio
import time
import logging
from unittest.mock import AsyncMock, MagicMock

import config
import quiz_bot
from quiz_bot import run_quiz_session

# Setup logging
logging.basicConfig(level=logging.INFO)

async def run_timing_benchmark():
    print("=" * 60)
    print("🚀 RUNNING 10-QUESTION QUIZ TIMING BENCHMARK TEST")
    print("=" * 60)

    # Mock Telegram Bot
    mock_bot = MagicMock()
    
    # Store poll creation timestamps
    poll_created_timestamps = []
    poll_id_counter = [0]

    async def mock_send_poll(chat_id, question, options, type, correct_option_id, is_anonymous, open_period):
        # Simulate realistic Telegram API network RTT (20ms)
        await asyncio.sleep(0.02)
        t_created = time.monotonic()
        poll_created_timestamps.append(t_created)
        
        poll_id_counter[0] += 1
        msg = MagicMock()
        msg.message_id = 1000 + poll_id_counter[0]
        msg.poll = MagicMock()
        msg.poll.id = f"poll_{poll_id_counter[0]}"
        msg.date = MagicMock()
        msg.date.timestamp.return_value = time.time()
        return msg

    async def mock_send_message(chat_id, text):
        # Simulate realistic Telegram message send API RTT (15ms)
        await asyncio.sleep(0.015)
        msg = MagicMock()
        msg.message_id = 2000
        return msg

    async def mock_stop_poll(chat_id, message_id):
        await asyncio.sleep(0.01)
        return MagicMock()

    mock_bot.send_poll = AsyncMock(side_effect=mock_send_poll)
    mock_bot.send_message = AsyncMock(side_effect=mock_send_message)
    mock_bot.stop_poll = AsyncMock(side_effect=mock_stop_poll)

    # 10 Questions matching specified distribution:
    # 1. Normal
    # 2. Normal
    # 3. Long (>200 chars)
    # 4. Normal
    # 5. Long (>200 chars)
    # 6. Section transition boundary (Start of Sec 2)
    # 7. Normal
    # 8. Long (>200 chars)
    # 9. Normal
    # 10. Final Question
    long_text = "What is the primary function of the mitochondria in eukaryotic cells? " + ("A " * 150)
    
    questions = [
        {"question_text": "Q1 Normal Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 0},
        {"question_text": "Q2 Normal Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 1},
        {"question_text": f"Q3 Long Question: {long_text}", "options": ["A", "B", "C", "D"], "correct_option_id": 2},
        {"question_text": "Q4 Normal Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 3},
        {"question_text": f"Q5 Long Question: {long_text}", "options": ["A", "B", "C", "D"], "correct_option_id": 0},
        {"question_text": "Q6 Section 2 Start Question?", "options": ["A", "B", "C", "D"], "correct_option_id": 1},
        {"question_text": "Q7 Normal Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 2},
        {"question_text": f"Q8 Long Question: {long_text}", "options": ["A", "B", "C", "D"], "correct_option_id": 3},
        {"question_text": "Q9 Normal Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 0},
        {"question_text": "Q10 Final Question Text?", "options": ["A", "B", "C", "D"], "correct_option_id": 1},
    ]

    timer_duration = 0.5  # 0.5s per question for fast automated test run
    quiz_data = {
        "quiz_id": "TEST_TIMING_BENCHMARK",
        "name": "Timing Diagnostic Benchmark Quiz",
        "timer": timer_duration,
        "questions": questions,
        "sections_enabled": 1,
        "sections": [
            {"name": "Section 1: Basics", "start": 1, "end": 5},
            {"name": "Section 2: Advanced", "start": 6, "end": 10}
        ]
    }

    t_quiz_start = time.monotonic()
    await run_quiz_session(mock_bot, -100123456789, quiz_data)
    t_quiz_end = time.monotonic()

    print("\n" + "=" * 60)
    print("📊 BENCHMARK TIMING RESULTS FOR 10 CONSECUTIVE QUESTIONS")
    print("=" * 60)

    gaps = []
    has_anomaly = False

    for idx in range(len(poll_created_timestamps)):
        q_num = idx + 1
        t_create = poll_created_timestamps[idx]
        
        if idx == 0:
            print(f"Q1 created at +{((t_create - t_quiz_start)*1000):.2f}ms from quiz start")
        else:
            prev_create = poll_created_timestamps[idx - 1]
            prev_finish = prev_create + timer_duration
            gap_sec = t_create - prev_finish
            gap_ms = gap_sec * 1000.0
            gaps.append(gap_ms)

            q_type_desc = ""
            if q_num in [3, 5, 8]:
                q_type_desc = "[LONG QUESTION]"
            elif q_num == 6:
                q_type_desc = "[SECTION TRANSITION]"
            else:
                q_type_desc = "[NORMAL]"

            status = "✅ PASS"
            threshold = 500.0 if ("LONG" in q_type_desc or "SECTION" in q_type_desc) else 300.0
            if gap_ms > threshold:
                status = "❌ FAIL (EXCEEDED THRESHOLD)"
                has_anomaly = True

            print(f"Q{q_num - 1} → Q{q_num} {q_type_desc:<20}: Gap = {gap_ms:.2f} ms ({gap_sec:.3f} s) | {status}")

    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    max_gap = max(gaps) if gaps else 0

    print("-" * 60)
    print(f"Average Transition Gap: {avg_gap:.2f} ms")
    print(f"Maximum Transition Gap: {max_gap:.2f} ms")
    print("=" * 60)

    assert not has_anomaly, f"Timing benchmark failed! Max gap was {max_gap:.2f}ms"
    print("🎉 ALL TRANSITION TIMINGS PASSED ZERO-DRIFT THRESHOLDS SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(run_timing_benchmark())
