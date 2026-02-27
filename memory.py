import sqlite3
import json
from datetime import datetime

DB_PATH = "chat_memory.db"


class ConversationMemory:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # messages table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TEXT
        )
        """)

        # sessions table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            run_id TEXT,
            session_state TEXT,
            final_output TEXT,
            created_at TEXT
        )
        """)

        conn.commit()
        conn.close()

    def create_session(self, session_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at) VALUES (?, ?)",
            (session_id, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

    def get_sessions(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM sessions ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def delete_session(self, session_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        conn.commit()
        conn.close()

    def get_history(self, session_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC",
            (session_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{"role": row[0], "content": row[1]} for row in rows]

    def add_message(self, session_id, role, content):
        self.create_session(session_id)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

    def save_execution(self, session_id, run_id, session_state, final_output):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO executions (session_id, run_id, session_state, final_output, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        """, (session_id, run_id, json.dumps(session_state), final_output))

        conn.commit()
        conn.close()


memory = ConversationMemory()