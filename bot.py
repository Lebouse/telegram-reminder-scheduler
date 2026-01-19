# bot.py

import asyncio
import re
import datetime
import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters, CallbackQueryHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import BOT_TOKEN, AUTHORIZED_USER_IDS, TIMEZONE
from database import init_db, add_scheduled_message, get_all_active_messages, get_message_by_id, deactivate_message
from scheduler_logic import publish_message_and_reschedule

WAITING_CONTENT, SELECT_CHAT, INPUT_DATE, SELECT_RECURRENCE, SELECT_PIN, SELECT_NOTIFY, SELECT_DELETE_DAYS = range(7)
EDIT_SELECT_FIELD, EDIT_WAITING_VALUE = range(7, 9)

user_sessions = {}
local_tz = pytz.timezone(TIMEZONE)

# === Вспомогательные функции ===

def check_auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in AUTHORIZED_USER_IDS:
            await update.message.reply_text("❌ Доступ запрещён.")
            return
        return await func(update, context)
    return wrapper

def format_message_row(row):
    _, chat_id, text, photo, doc, caption, pub_at, _, rec, pin, notify, del_days, _ = row
    content = caption or text or ("🖼 Фото" if photo else "📄 PDF")
    rec_map = {'once': 'Один раз', 'daily': 'Ежедневно', 'weekly': 'Еженедельно', 'monthly': 'Ежемесячно'}
    return (
        f"ID: {row[0]}\n"
        f"Чат: {chat_id}\n"
        f"Контент: {content[:50]}...\n"
        f"Публикация: {pub_at}\n"
        f"Повтор: {rec_map.get(rec, rec)}\n"
        f"Закреп: {'Да' if pin else 'Нет'} | Удалить через: {del_days or '—'} дн.\n"
        f"{'—' * 30}"
    )

# === Команды ===

@check_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправьте сообщение (текст/фото/PDF) для планирования."
    )
    return WAITING_CONTENT

# ... (receive_content, select_chat, input_date — без изменений, см. предыдущую версию)

async def receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in AUTHORIZED_USER_IDS:
        return

    session = {'text': None, 'photo_file_id': None, 'document_file_id': None, 'caption': None}

    if update.message.text:
        session['text'] = update.message.text
    elif update.message.photo:
        session['photo_file_id'] = update.message.photo[-1].file_id
        session['caption'] = update.message.caption
    elif update.message.document:
        if update.message.document.mime_type in ('application/pdf',):
            session['document_file_id'] = update.message.document.file_id
            session['caption'] = update.message.caption
        else:
            await update.message.reply_text("Поддерживаются только PDF.")
            return WAITING_CONTENT
    else:
        await update.message.reply_text("Текст, фото или PDF.")
        return WAITING_CONTENT

    user_sessions[user_id] = session
    await update.message.reply_text("Введите ID чата (например, -1001234567890):")
    return SELECT_CHAT

async def select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        chat_id = int(update.message.text.strip())
        user_sessions[user_id]['chat_id'] = chat_id
    except ValueError:
        await update.message.reply_text("Неверный ID чата.")
        return SELECT_CHAT
    await update.message.reply_text("Дата публикации (ДД.ММ.ГГГГ ЧЧ:ММ):")
    return INPUT_DATE

async def input_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", text)
    if not match:
        await update.message.reply_text("Формат: ДД.ММ.ГГГГ ЧЧ:ММ")
        return INPUT_DATE

    day, month, year, hour, minute = map(int, match.groups())
    try:
        naive_dt = datetime.datetime(year, month, day, hour, minute)
        local_dt = local_tz.localize(naive_dt)
        utc_dt = local_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        if utc_dt <= datetime.datetime.utcnow():
            await update.message.reply_text("Дата должна быть в будущем!")
            return INPUT_DATE
        user_sessions[user_id]['publish_at'] = utc_dt.isoformat()
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return INPUT_DATE

    keyboard = [
        [InlineKeyboardButton("Один раз", callback_data="once")],
        [InlineKeyboardButton("Ежедневно", callback_data="daily")],
        [InlineKeyboardButton("Еженедельно", callback_data="weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="monthly")]
    ]
    await update.message.reply_text("Периодичность:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_RECURRENCE

# ... (select_recurrence, select_pin, select_notify, select_delete_days — как раньше)

async def select_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_sessions[user_id]['recurrence'] = query.data
    keyboard = [[InlineKeyboardButton("Да", callback_data="1"), InlineKeyboardButton("Нет", callback_data="0")]]
    await query.edit_message_text("Закрепить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_PIN

async def select_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_sessions[user_id]['pin'] = bool(int(query.data))
    keyboard = [[InlineKeyboardButton("Да", callback_data="1"), InlineKeyboardButton("Нет", callback_data="0")]]
    await query.edit_message_text("Оповестить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_NOTIFY

async def select_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_sessions[user_id]['notify'] = bool(int(query.data))
    keyboard = [
        [InlineKeyboardButton("1 день", callback_data="1")],
        [InlineKeyboardButton("2 дня", callback_data="2")],
        [InlineKeyboardButton("3 дня", callback_data="3")],
        [InlineKeyboardButton("Никогда", callback_data="0")]
    ]
    await query.edit_message_text("Удалить через:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_DELETE_DAYS

async def select_delete_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    days = int(query.data)
    user_sessions[user_id]['delete_after_days'] = days if days > 0 else None
    data = user_sessions[user_id]
    msg_id = add_scheduled_message(data)
    await query.edit_message_text(f"✅ Задача создана! ID: {msg_id}")
    schedule_all_jobs(context.application.job_queue)
    del user_sessions[user_id]
    return ConversationHandler.END

@check_auth
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_all_active_messages()
    if not tasks:
        await update.message.reply_text("Нет активных задач.")
        return
    text = "\n".join(format_message_row(t) for t in tasks)
    await update.message.reply_text(f"Активные задачи:\n\n{text}")

@check_auth
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Используйте: /delete <id>")
        return
    try:
        msg_id = int(context.args[0])
        row = get_message_by_id(msg_id)
        if not row:
            await update.message.reply_text("Задача не найдена.")
            return
        deactivate_message(msg_id)
        await update.message.reply_text(f"Задача {msg_id} удалена.")
        schedule_all_jobs(context.application.job_queue)
    except ValueError:
        await update.message.reply_text("Неверный ID.")

# === Планировщик ===

def schedule_all_jobs(job_queue):
    job_queue.scheduler.remove_all_jobs()
    messages = get_all_active_messages()
    for row in messages:
        msg_id, chat_id, text, photo, doc, caption, publish_at_str, _, recurrence, pin, notify, del_days, _ = row
        publish_at = datetime.datetime.fromisoformat(publish_at_str)
        if publish_at > datetime.datetime.utcnow():
            job_queue.run_once(
                lambda ctx, r=row: publish_message_and_reschedule(
                    ctx, r[0], r[1], r[2], r[3], r[4], r[5], r[8], r[9], r[10], r[11], r[6]
                ),
                publish_at
            )

# === Запуск ===

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.start()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_content)],
            SELECT_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_chat)],
            INPUT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_date)],
            SELECT_RECURRENCE: [CallbackQueryHandler(select_recurrence)],
            SELECT_PIN: [CallbackQueryHandler(select_pin)],
            SELECT_NOTIFY: [CallbackQueryHandler(select_notify)],
            SELECT_DELETE_DAYS: [CallbackQueryHandler(select_delete_days)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Отменено."))]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("delete", delete_task))
    # /edit можно реализовать аналогично — по желанию

    app.job_queue.scheduler = scheduler
    schedule_all_jobs(app.job_queue)

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
