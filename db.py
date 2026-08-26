import os
import json
import random
import string
import logging
from typing import Dict, Any, List, Optional

import config

logger = logging.getLogger(__name__)

# Determine database mode: MongoDB Atlas if MONGODB_URI is provided, otherwise local SQLite
USE_MONGODB = bool(config.MONGODB_URI and config.MONGODB_URI.strip())

mongo_client = None
mongo_db = None

if USE_MONGODB:
    try:
        import pymongo
        mongo_client = pymongo.MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Test connection
        mongo_client.admin.command('ping')
        mongo_db = mongo_client.get_database("telegram_quiz_bot")
        logger.info("Connected successfully to MongoDB Atlas database.")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}. Falling back to SQLite.")
        USE_MONGODB = False

DB_PATH = os.path.join(os.path.dirname(__file__), "quiz_bot.db")

def init_db():
    """Initializes SQLite tables if running in SQLite mode."""
    if USE_MONGODB:
        # Create indexes for MongoDB
        try:
            mongo_db.quizzes.create_index("quiz_id", unique=True)
            mongo_db.quizzes.create_index("user_id")
            mongo_db.schedules.create_index("quiz_id", unique=True)
            logger.info("MongoDB indexes verified.")
        except Exception as e:
            logger.error(f"Error creating MongoDB indexes: {e}")
        return

    import sqlite3
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
    
    # Check and perform migrations for existing SQLite DB if any
    cursor.execute("PRAGMA table_info(quizzes)")
    columns = [info[1] for info in cursor.fetchall()]
    if "user_id" not in columns:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN user_id INTEGER DEFAULT 0")
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
        cursor.execute("ALTER TABLE schedules ADD COLUMN user_id INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

def generate_quiz_id() -> str:
    """Generates unique Quiz ID in GG format (e.g. GGX7K9P2A)."""
    chars = string.ascii_uppercase + string.digits
    random_str = ''.join(random.choices(chars, k=7))
    return f"GG{random_str}"

def save_quiz(user_id: int, name: str, timer: int, questions: list, creator_name: str = "User", sections_enabled: int = 0, sections: list = None) -> str:
    if sections is None:
        sections = []
    quiz_id = generate_quiz_id()

    if USE_MONGODB:
        doc = {
            "quiz_id": quiz_id,
            "user_id": int(user_id),
            "name": name,
            "timer": timer,
            "questions": questions,
            "creator_name": creator_name,
            "sections_enabled": sections_enabled,
            "sections": sections,
            "created_at": os.popen("date /t").read().strip() if os.name == 'nt' else ""
        }
        mongo_db.quizzes.insert_one(doc)
        return quiz_id

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO quizzes (quiz_id, user_id, name, timer, questions_json, creator_name, sections_enabled, sections_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (quiz_id, int(user_id), name, timer, json.dumps(questions, ensure_ascii=False), creator_name, sections_enabled, json.dumps(sections, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return quiz_id

def get_quiz(quiz_id: str) -> Optional[Dict[str, Any]]:
    if USE_MONGODB:
        doc = mongo_db.quizzes.find_one({"quiz_id": quiz_id})
        if doc:
            doc.pop("_id", None)
            return doc
        return None

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quiz_id, name, timer, questions_json, created_at, creator_name, sections_enabled, sections_json, user_id
        FROM quizzes WHERE quiz_id = ?
    """, (quiz_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        sec_enabled = row[6] if len(row) > 6 and row[6] is not None else 0
        sec_list = json.loads(row[7]) if len(row) > 7 and row[7] is not None else []
        u_id = row[8] if len(row) > 8 and row[8] is not None else 0
        return {
            "quiz_id": row[0],
            "name": row[1],
            "timer": row[2],
            "questions": json.loads(row[3]),
            "created_at": row[4],
            "creator_name": row[5],
            "sections_enabled": sec_enabled,
            "sections": sec_list,
            "user_id": u_id
        }
    return None

def get_user_quizzes(user_id: int) -> List[Dict[str, Any]]:
    """Fetches all quizzes created by a specific user for multi-user security isolation."""
    if USE_MONGODB:
        cursor = mongo_db.quizzes.find({"user_id": int(user_id)})
        results = []
        for doc in cursor:
            doc.pop("_id", None)
            results.append(doc)
        return results

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quiz_id, name, timer, questions_json, created_at, creator_name, sections_enabled, sections_json, user_id
        FROM quizzes WHERE user_id = ? ORDER BY rowid DESC
    """, (int(user_id),))
    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        results.append({
            "quiz_id": row[0],
            "name": row[1],
            "timer": row[2],
            "questions": json.loads(row[3]),
            "created_at": row[4],
            "creator_name": row[5],
            "sections_enabled": row[6] if len(row) > 6 and row[6] is not None else 0,
            "sections": json.loads(row[7]) if len(row) > 7 and row[7] is not None else [],
            "user_id": row[8]
        })
    return results

def delete_quiz(quiz_id: str, user_id: int) -> bool:
    """Deletes quiz if the user_id matches ownership."""
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.delete_one({"quiz_id": quiz_id, "user_id": int(user_id)})
        return res.deleted_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM quizzes WHERE quiz_id = ? AND user_id = ?", (quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_quiz_name(quiz_id: str, user_id: int, new_name: str) -> bool:
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"name": new_name}})
        return res.modified_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET name = ? WHERE quiz_id = ? AND user_id = ?", (new_name, quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_quiz_timer(quiz_id: str, user_id: int, new_timer: int) -> bool:
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"timer": new_timer}})
        return res.modified_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET timer = ? WHERE quiz_id = ? AND user_id = ?", (new_timer, quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_quiz_questions(quiz_id: str, user_id: int, questions: list) -> bool:
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"questions": questions}})
        return res.modified_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET questions_json = ? WHERE quiz_id = ? AND user_id = ?", (json.dumps(questions, ensure_ascii=False), quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_quiz_sections_enabled(quiz_id: str, user_id: int, enabled: int) -> bool:
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"sections_enabled": enabled}})
        return res.modified_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET sections_enabled = ? WHERE quiz_id = ? AND user_id = ?", (enabled, quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_quiz_sections(quiz_id: str, user_id: int, sections: list) -> bool:
    quiz = get_quiz(quiz_id)
    if not quiz or quiz.get("user_id") != int(user_id):
        return False

    if USE_MONGODB:
        res = mongo_db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"sections": sections}})
        return res.modified_count > 0

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET sections_json = ? WHERE quiz_id = ? AND user_id = ?", (json.dumps(sections, ensure_ascii=False), quiz_id, int(user_id)))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def save_schedule(quiz_id: str, user_id: int, scheduled_timestamp: float, time_str: str):
    if USE_MONGODB:
        mongo_db.schedules.update_one(
            {"quiz_id": quiz_id},
            {"$set": {"quiz_id": quiz_id, "user_id": int(user_id), "scheduled_timestamp": scheduled_timestamp, "time_str": time_str}},
            upsert=True
        )
        return

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO schedules (quiz_id, user_id, scheduled_timestamp, time_str)
        VALUES (?, ?, ?, ?)
    """, (quiz_id, int(user_id), scheduled_timestamp, time_str))
    conn.commit()
    conn.close()

def get_active_schedules() -> List[Dict[str, Any]]:
    if USE_MONGODB:
        cursor = mongo_db.schedules.find({})
        results = []
        for doc in cursor:
            doc.pop("_id", None)
            results.append(doc)
        return results

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT quiz_id, scheduled_timestamp, time_str, user_id FROM schedules")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "quiz_id": r[0],
            "scheduled_timestamp": r[1],
            "time_str": r[2],
            "user_id": r[3] if len(r) > 3 else 0
        }
        for r in rows
    ]

def delete_schedule(quiz_id: str, user_id: Optional[int] = None):
    if USE_MONGODB:
        query = {"quiz_id": quiz_id}
        if user_id is not None:
            query["user_id"] = int(user_id)
        mongo_db.schedules.delete_one(query)
        return

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("DELETE FROM schedules WHERE quiz_id = ? AND user_id = ?", (quiz_id, int(user_id)))
    else:
        cursor.execute("DELETE FROM schedules WHERE quiz_id = ?", (quiz_id,))
    conn.commit()
    conn.close()
