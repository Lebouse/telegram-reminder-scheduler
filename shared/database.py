# shared/database.py

import sqlite3
import threading
from contextlib import contextmanager
from config import DATABASE_PATH

# Глобальный lock для SQLite (на всякий случай)
_db_lock = threading.RLock()

def init_db():
    with get_db_connection() as conn:
        conn.execute('PRAGMA journal_mode=WAL;')  # Включаем WAL для concurrency
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT,
                photo_file_id TEXT,
                document_file_id TEXT,
                caption TEXT,
                publish_at TEXT NOT NULL,
                original_publish_at TEXT NOT NULL,
                recurrence TEXT NOT NULL DEFAULT 'once',
                pin BOOLEAN NOT NULL DEFAULT 0,
                notify BOOLEAN NOT NULL DEFAULT 1,
                delete_after_days INTEGER,
                active BOOLEAN NOT NULL DEFAULT 1
            )
        ''')
        conn.commit()

@contextmanager
def get_db_connection():
    with _db_lock:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=20)
        conn.execute('PRAGMA busy_timeout = 20000;')
        try:
            yield conn
        finally:
            conn.close()

def add_scheduled_message(data: dict) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO scheduled_messages
            (chat_id, text, photo_file_id, document_file_id, caption, publish_at,
             original_publish_at, recurrence, pin, notify, delete_after_days, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['chat_id'], data['text'], data['photo_file_id'], data['document_file_id'],
            data['caption'], data['publish_at'], data['publish_at'], data['recurrence'],
            data['pin'], data['notify'], data['delete_after_days'], True
        ))
        msg_id = cursor.lastrowid
        conn.commit()
        return msg_id

def get_all_active_messages():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scheduled_messages WHERE active = 1 ORDER BY publish_at")
        rows = cursor.fetchall()
        return rows

def deactivate_message(msg_id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE scheduled_messages SET active = 0 WHERE id = ?", (msg_id,))
        conn.commit()

def update_next_publish_time(msg_id: int, next_time_iso: str):
    with get_db_connection() as conn:
        conn.execute("UPDATE scheduled_messages SET publish_at = ? WHERE id = ?", (next_time_iso, msg_id))
        conn.commit()
