# shared/database.py

import sqlite3
import threading
import datetime
import logging
from contextlib import contextmanager
from typing import Optional, List, Tuple
from config import DATABASE_PATH
from shared.utils import generate_task_hash

logger = logging.getLogger(__name__)

# Глобальный lock для SQLite (на случай многопоточности)
_db_lock = threading.RLock()


def init_db():
    """Инициализирует базу данных и создаёт таблицу при необходимости."""
    with get_db_connection() as conn:
        conn.execute('PRAGMA journal_mode=WAL;')  # Включаем WAL для concurrency
        conn.execute('PRAGMA foreign_keys = ON;')
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
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TEXT NOT t NULL DEFAULT (datetime('now')),
                max_end_date TEXT,
                task_hash TEXT
            )
        ''')
        conn.commit()
        logger.info("База данных инициализирована.")


@contextmanager
def get_db_connection():
    """Контекстный менеджер для безопасного подключения к SQLite."""
    with _db_lock:
        conn = sqlite3.connect(
            DATABASE_PATH,
            check_same_thread=False,
            timeout=20
        )
        conn.execute('PRAGMA busy_timeout = 20000;')
        try:
            yield conn
        finally:
            conn.close()


def add_scheduled_message(data: dict) -> int:
    """
    Добавляет новую запланированную задачу.
    Автоматически добавляет недостающие столбцы при первой миграции.
    """
    # Генерируем хэш для защиты от дублей
    task_hash = generate_task_hash(
        chat_id=data['chat_id'],
        text=data['text'],
        photo_file_id=data['photo_file_id'],
        document_file_id=data['document_file_id'],
        publish_at=data['publish_at'],
        recurrence=data['recurrence']
    )

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Проверка на дубликат
        cursor.execute(
            "SELECT id FROM scheduled_messages WHERE task_hash = ? AND active = 1",
            (task_hash,)
        )
        existing = cursor.fetchone()
        if existing:
            raise ValueError(f"Такая задача уже запланирована (ID: {existing[0]})")

        # Подготавливаем данные
        created_at = datetime.datetime.utcnow().isoformat()
        max_end_date = (datetime.datetime.utcnow() + datetime.timedelta(days=365)).isoformat()

        # Пытаемся вставить с полным набором столбцов
        try:
            cursor.execute('''
                INSERT INTO scheduled_messages
                (chat_id, text, photo_file_id, document_file_id, caption, publish_at,
                 original_publish_at, recurrence, pin, notify, delete_after_days, active,
                 created_at, max_end_date, task_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['chat_id'], data['text'], data['photo_file_id'], data['document_file_id'],
                data['caption'], data['publish_at'], data['publish_at'], data['recurrence'],
                data['pin'], data['notify'], data['delete_after_days'], True,
                created_at, max_end_date, task_hash
            ))
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                # Миграция: добавляем недостающие столбцы
                try:
                    cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN task_hash TEXT")
                except sqlite3.OperationalError:
                    pass  # уже существует
                try:
                    cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN max_end_date TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")
                except sqlite3.OperationalError:
                    pass

                conn.commit()

                # Повторяем вставку
                cursor.execute('''
                    INSERT INTO scheduled_messages
                    (chat_id, text, photo_file_id, document_file_id, caption, publish_at,
                     original_publish_at, recurrence, pin, notify, delete_after_days, active,
                     created_at, max_end_date, task_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data['chat_id'], data['text'], data['photo_file_id'], data['document_file_id'],
                    data['caption'], data['publish_at'], data['publish_at'], data['recurrence'],
                    data['pin'], data['notify'], data['delete_after_days'], True,
                    created_at, max_end_date, task_hash
                ))
            else:
                raise

        msg_id = cursor.lastrowid
        conn.commit()
        logger.info(f"Создана задача ID={msg_id} с хэшем {task_hash}")
        return msg_id


def get_all_active_messages() -> List[Tuple]:
    """Возвращает все активные задачи."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, chat_id, text, photo_file_id, document_file_id, caption,
                   publish_at, original_publish_at, recurrence, pin, notify,
                   delete_after_days, active, created_at, max_end_date
            FROM scheduled_messages
            WHERE active = 1
            ORDER BY publish_at
        """)
        rows = cursor.fetchall()
        logger.debug(f"Загружено {len(rows)} активных задач")
        return rows


def get_message_by_id(msg_id: int) -> Optional[Tuple]:
    """Возвращает задачу по ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, chat_id, text, photo_file_id, document_file_id, caption,
                   publish_at, original_publish_at, recurrence, pin, notify,
                   delete_after_days, active, created_at, max_end_date
            FROM scheduled_messages
            WHERE id = ?
        """, (msg_id,))
        return cursor.fetchone()


def deactivate_message(msg_id: int):
    """Деактивирует задачу (логическое удаление)."""
    with get_db_connection() as conn:
        conn.execute("UPDATE scheduled_messages SET active = 0 WHERE id = ?", (msg_id,))
        conn.commit()
        logger.info(f"Задача ID={msg_id} деактивирована")


def update_scheduled_message(
    msg_id: int,
    chat_id: int,
    text: Optional[str],
    photo_file_id: Optional[str],
    document_file_id: Optional[str],
    caption: Optional[str],
    publish_at: str,
    recurrence: str,
    pin: bool,
    notify: bool,
    delete_after_days: Optional[int]
):
    """Обновляет существующую задачу и сбрасывает срок действия."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        max_end_date = (datetime.datetime.utcnow() + datetime.timedelta(days=365)).isoformat()
        cursor.execute('''
            UPDATE scheduled_messages SET
                chat_id = ?, text = ?, photo_file_id = ?, document_file_id = ?,
                caption = ?, publish_at = ?, recurrence = ?, pin = ?, notify = ?,
                delete_after_days = ?, max_end_date = ?
            WHERE id = ?
        ''', (
            chat_id, text, photo_file_id, document_file_id,
            caption, publish_at, recurrence, pin, notify,
            delete_after_days, max_end_date, msg_id
        ))
        if cursor.rowcount == 0:
            raise ValueError("Задача не найдена")
        conn.commit()
        logger.info(f"Задача ID={msg_id} обновлена")


def update_next_publish_time(msg_id: int, next_time_iso: str):
    """Обновляет время следующей публикации."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active FROM scheduled_messages WHERE id = ?", (msg_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            logger.warning(f"Попытка обновить неактивную задачу {msg_id}")
            return
        cursor.execute("UPDATE scheduled_messages SET publish_at = ? WHERE id = ?", (next_time_iso, msg_id))
        conn.commit()
        logger.debug(f"Задача {msg_id}: следующая публикация назначена на {next_time_iso}")


def cleanup_old_tasks(max_age_days: int = 30) -> int:
    """
    Удаляет неактивные задачи старше max_age_days.
    Возвращает количество удалённых записей.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
    with get_db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM scheduled_messages WHERE active = 0 AND created_at < ?",
            (cutoff.isoformat(),)
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"Очистка: удалено {deleted} старых задач")
        return deleted
