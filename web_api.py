# web_api.py
# Полностью рабочая версия для v0.1.0-pre
# Сервер: 178.255.127.155
# Порт: 8081
# Секрет админки: qwerty12345

import asyncio
import datetime
import csv
import io
import logging
import os
from typing import Optional, List, Dict, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Header, Request, Form, status, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, validator
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from config import WEB_API_SECRET, ADMIN_SECRET, BOT_TOKEN, TIMEZONE
from shared.database import (
    get_all_active_messages, deactivate_message,
    update_scheduled_message, add_scheduled_message
)
from shared.utils import (
    escape_markdown_v2, detect_media_type,
    parse_user_datetime
)
from scheduler_logic import publish_message

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# === Инициализация FastAPI ===
app = FastAPI(title="Telegram Reminder Scheduler API")

# === Метрики Prometheus ===
TASKS_CREATED = Counter('telegram_scheduler_tasks_created_total', 'Total tasks created')
TASKS_DELETED = Counter('telegram_scheduler_tasks_deleted_total', 'Total tasks deleted')
ACTIVE_TASKS = Gauge('telegram_scheduler_active_tasks', 'Number of active scheduled tasks')

# === Шаблоны ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# === Кэш названий чатов ===
CHAT_TITLE_CACHE: Dict[int, tuple] = {}

# === Модели данных ===
class PublishRequest(BaseModel):
    chat_id: int
    text: Optional[str] = None
    photo_file_id: Optional[str] = None
    document_file_id: Optional[str] = None
    caption: Optional[str] = None
    pin: bool = False
    notify: bool = True
    delete_after_days: Optional[int] = None

    @validator('delete_after_days')
    def validate_delete_days(cls, v):
        if v is not None and v not in (1, 2, 3):
            raise ValueError('Must be 1, 2, or 3')
        return v

# === Вспомогательные функции ===
async def get_chat_title(chat_id: int) -> str:
    """Получает название чата через Telegram API с кэшированием."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if chat_id in CHAT_TITLE_CACHE:
        title, timestamp = CHAT_TITLE_CACHE[chat_id]
        if (now - timestamp).total_seconds() < 3600:  # кэш 1 час
            return title

    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        chat = await bot.get_chat(chat_id)
        title = chat.title or f"Чат {chat_id}"
    except Exception as e:
        logger.warning(f"Не удалось получить название чата {chat_id}: {e}")
        title = f"Чат {chat_id}"

    CHAT_TITLE_CACHE[chat_id] = (title, now)
    return title

# === Эндпоинты ===

@app.get("/health", summary="Health check")
async def health_check():
    """Проверяет работоспособность сервиса."""
    try:
        tasks = get_all_active_messages()
        return JSONResponse({
            "status": "ok",
            "active_tasks": len(tasks),
            "timestamp": datetime.datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            {"status": "error", "detail": str(e)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@app.get("/metrics", summary="Prometheus metrics")
async def metrics():
    """Экспортирует метрики для Prometheus."""
    active_count = len(get_all_active_messages())
    ACTIVE_TASKS.set(active_count)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/publish", summary="Publish message immediately")
async def web_publish(request: PublishRequest, x_secret: str = Header(...)):
    """Публикует сообщение немедленно через HTTP API."""
    if WEB_API_SECRET and x_secret != WEB_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        # Экранируем текст для MarkdownV2
        safe_text = escape_markdown_v2(request.text) if request.text else None
        safe_caption = escape_markdown_v2(request.caption) if request.caption else None

        msg_id = await publish_message(
            chat_id=request.chat_id,
            text=safe_text,
            photo_file_id=request.photo_file_id,
            document_file_id=request.document_file_id,
            caption=safe_caption,
            pin=request.pin,
            notify=request.notify,
            delete_after_days=request.delete_after_days
        )
        if msg_id is None:
            raise HTTPException(status_code=500, detail="Failed to send message")
        logger.info(f"Web publish: chat={request.chat_id}, msg_id={msg_id}")
        return {"ok": True, "message_id": msg_id}
    except Exception as e:
        logger.exception("Web publish error")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin", summary="Admin panel")
async def admin_panel(
    request: Request,
    chat_filter: Optional[str] = None,
    secret: Optional[str] = Query(None),  # Явно указываем Query параметр
    x_admin_secret: str = Header(None, alias="X-Admin-Secret")
):
    """
    Отображает админку для управления задачами.
    Поддерживает секрет как из заголовка, так и из URL параметра.
    """
    # ДЕТАЛЬНАЯ ОТЛАДКА (временно для настройки)
    logger.info("=" * 60)
    logger.info(f"📥 ЗАПРОС К /admin")
    logger.info(f"Полный URL: {request.url}")
    logger.info(f"Параметры URL: {dict(request.query_params)}")
    logger.info(f"Заголовки запроса: {dict(request.headers)}")
    logger.info(f"ADMIN_SECRET из конфига: '{ADMIN_SECRET}'")
    logger.info(f"secret из URL: '{secret}'")
    logger.info(f"X-Admin-Secret из заголовка: '{x_admin_secret}'")

    # Комбинируем источники секрета
    actual_secret = x_admin_secret or secret or request.query_params.get("secret")
    
    logger.info(f"🔍 Итоговый секрет для проверки: '{actual_secret}'")

    # Проверка доступа
    if ADMIN_SECRET and str(actual_secret) != str(ADMIN_SECRET):
        logger.error("❌ ДОСТУП ЗАПРЕЩЁН! Секреты не совпадают")
        logger.error(f"Ожидалось: '{ADMIN_SECRET}'")
        logger.error(f"Получено: '{actual_secret}'")
        raise HTTPException(status_code=403, detail="Admin access required")
    
    logger.info("✅ ДОСТУП РАЗРЕШЁН!")

    tasks = get_all_active_messages()

    # Фильтрация по чату
    if chat_filter and chat_filter.lstrip('-').isdigit():
        chat_filter = int(chat_filter)
        tasks = [t for t in tasks if t[1] == chat_filter]

    # Уникальные чаты
    unique_chats = sorted({t[1] for t in tasks})
    chat_titles = {}
    for cid in unique_chats:
        chat_titles[cid] = await get_chat_title(cid)

    # Преобразуем кортежи в словари
    task_dicts = []
    for row in tasks:
        task_dicts.append({
            'id': row[0],
            'chat_id': row[1],
            'text': row[2],
            'photo_file_id': row[3],
            'document_file_id': row[4],
            'caption': row[5],
            'publish_at': row[6],
            'recurrence': row[8],
            'pin': bool(row[9]),
            'notify': bool(row[10]),
            'delete_after_days': row[11],
            'active': row[12]
        })

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "tasks": task_dicts,
        "active_count": len(tasks),
        "unique_chats": unique_chats,
        "chat_titles": chat_titles,
        "chat_filter": chat_filter,
        "timezone": str(TIMEZONE)
    })

@app.post("/admin/delete/{task_id}", summary="Delete task")
async def admin_delete_task(task_id: int, x_admin_secret: str = Header(None)):
    """Удаляет задачу."""
    if ADMIN_SECRET and x_admin_secret != ADMIN_SECRET:
        logger.warning(f"Попытка удаления без прав: task_id={task_id}")
        raise HTTPException(status_code=403, detail="Admin access required")

    deactivate_message(task_id)
    TASKS_DELETED.inc()
    logger.info(f"Задача {task_id} удалена через админку")
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/export.csv", summary="Export tasks to CSV")
async def export_tasks_csv(x_admin_secret: str = Header(None)):
    """Экспортирует задачи в CSV."""
    if ADMIN_SECRET and x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Admin access required")

    tasks = get_all_active_messages()
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "Chat ID", "Text", "Photo file_id", "Document file_id", "Caption",
        "Publish At (UTC)", "Recurrence", "Pin", "Notify", "Delete After (days)"
    ])

    for row in tasks:
        writer.writerow([
            row[0], row[1], row[2], row[3], row[4], row[5],
            row[6], row[8], row[9], row[10], row[11]
        ])

    output.seek(0)
    filename = f"tasks_export_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={quote(filename)}"}
    )

# === Запуск сервера ===
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8081))  # Используем порт из переменной окружения
    logger.info(f"🚀 Запуск веб-API на порту {port}...")
    logger.info(f"🔐 ADMIN_SECRET: '{ADMIN_SECRET}'")
    uvicorn.run(app, host="0.0.0.0", port=port)
