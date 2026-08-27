import sqlite3
import json
import os
import random
import string

DB_PATH = os.path.join(os.path.dirname(__file__), "quiz_bot.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            quiz_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            timer INTEGER NOT NULL,
            questions_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            creator_name TEXT,
            sections_enabled INTEGER DEFAULT 0,
            sections_json TEXT DEFAULT '[]'
        )
    """)
    
    # Auto-migration for existing database schemas
    cursor.execute("PRAGMA table_info(quizzes)")
    columns = [info[1] for info in cursor.fetchall()]
    if "user_id" not in columns:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
    if "sections_enabled" not in columns:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN sections_enabled INTEGER DEFAULT 0")
    if "sections_json" not in columns:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN sections_json TEXT DEFAULT '[]'")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            quiz_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 0,
            scheduled_timestamp REAL NOT NULL,
            time_str TEXT NOT NULL
        )
    """)
    
    cursor.execute("PRAGMA table_info(schedules)")
    sched_cols = [info[1] for info in cursor.fetchall()]
    if "user_id" not in sched_cols:
        cursor.execute("ALTER TABLE schedules ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")

    conn.commit()
    conn.close()

def generate_quiz_id():
    # Format: GG + 7 alphanumeric characters
    chars = string.ascii_uppercase + string.digits
    return f"GG{''.join(random.choices(chars, k=7))}"

def save_quiz(user_id: int, name: str, timer: int, questions: list, creator_name: str = "User", sections_enabled: int = 0, sections: list = None) -> str:
    if sections is None:
        sections = []
    quiz_id = generate_quiz_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO quizzes (quiz_id, user_id, name, timer, questions_json, creator_name, sections_enabled, sections_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (quiz_id, user_id, name, timer, json.dumps(questions, ensure_ascii=False), creator_name, sections_enabled, json.dumps(sections, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return quiz_id

def get_quiz(quiz_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quiz_id, user_id, name, timer, questions_json, created_at, creator_name, sections_enabled, sections_json
        FROM quizzes WHERE quiz_id = ?
    """, (quiz_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        sec_enabled = row[7] if len(row) > 7 and row[7] is not None else 0
        sec_list = json.loads(row[8]) if len(row) > 8 and row[8] is not None else []
        return {
            "quiz_id": row[0],
            "user_id": row[1],
            "name": row[2],
            "timer": row[3],
            "questions": json.loads(row[4]),
            "created_at": row[5],
            "creator_name": row[6],
            "sections_enabled": sec_enabled,
            "sections": sec_list
        }
    return None

def get_user_quizzes(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quiz_id, user_id, name, timer, questions_json, created_at, creator_name, sections_enabled, sections_json
        FROM quizzes WHERE user_id = ? ORDER BY created_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    results = []
    for row in rows:
        sec_enabled = row[7] if len(row) > 7 and row[7] is not None else 0
        sec_list = json.loads(row[8]) if len(row) > 8 and row[8] is not None else []
        results.append({
            "quiz_id": row[0],
            "user_id": row[1],
            "name": row[2],
            "timer": row[3],
            "questions": json.loads(row[4]),
            "created_at": row[5],
            "creator_name": row[6],
            "sections_enabled": sec_enabled,
            "sections": sec_list
        })
    return results

def delete_quiz(quiz_id: str, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM quizzes WHERE quiz_id = ? AND user_id = ?", (quiz_id, user_id))
    conn.commit()
    conn.close()

def save_schedule(quiz_id: str, user_id: int, scheduled_timestamp: float, time_str: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO schedules (quiz_id, user_id, scheduled_timestamp, time_str)
        VALUES (?, ?, ?, ?)
    """, (quiz_id, user_id, scheduled_timestamp, time_str))
    conn.commit()
    conn.close()

def get_active_schedules():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT quiz_id, user_id, scheduled_timestamp, time_str FROM schedules")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "quiz_id": r[0],
            "user_id": r[1],
            "scheduled_timestamp": r[2],
            "time_str": r[3]
        }
        for r in rows
    ]

def delete_schedule(quiz_id: str, user_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if user_id:
        cursor.execute("DELETE FROM schedules WHERE quiz_id = ? AND user_id = ?", (quiz_id, user_id))
    else:
        cursor.execute("DELETE FROM schedules WHERE quiz_id = ?", (quiz_id,))
    conn.commit()
    conn.close()

def update_quiz_name(quiz_id: str, user_id: int, new_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET name = ? WHERE quiz_id = ? AND user_id = ?", (new_name, quiz_id, user_id))
    conn.commit()
    conn.close()

def update_quiz_timer(quiz_id: str, user_id: int, new_timer: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET timer = ? WHERE quiz_id = ? AND user_id = ?", (new_timer, quiz_id, user_id))
    conn.commit()
    conn.close()

def update_quiz_questions(quiz_id: str, user_id: int, questions: list):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET questions_json = ? WHERE quiz_id = ? AND user_id = ?", (json.dumps(questions, ensure_ascii=False), quiz_id, user_id))
    conn.commit()
    conn.close()

def update_quiz_sections_enabled(quiz_id: str, user_id: int, enabled: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET sections_enabled = ? WHERE quiz_id = ? AND user_id = ?", (enabled, quiz_id, user_id))
    conn.commit()
    conn.close()

def update_quiz_sections(quiz_id: str, user_id: int, sections: list):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET sections_json = ? WHERE quiz_id = ? AND user_id = ?", (json.dumps(sections, ensure_ascii=False), quiz_id, user_id))
    conn.commit()
    conn.close()
