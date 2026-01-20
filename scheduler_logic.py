# scheduler_logic.py
import logging
import datetime
import asyncio
from typing import Optional, Union, Tuple
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden

from config import BOT_TOKEN, TIMEZONE
from shared.database import (
    update_next_publish_time, deactivate_message, 
    get_message_by_id, get_all_active_messages
)
from shared.utils import (
    next_recurrence_time, detect_media_type, 
    escape_markdown_v2
)
from shared.bot_instance import get_bot

logger = logging.getLogger(__name__)

async def publish_message(
    chat_id: int,
    text: Optional[str] = None,
    photo_file_id: Optional[str] = None,
    document_file_id: Optional[str] = None,
    caption: Optional[str] = None,
    pin: bool = False,
    notify: bool = True,
    delete_after_days: Optional[int] = None
) -> Optional[int]:
    """
    Публикует сообщение в указанный чат.
    
    Args:
        chat_id: ID чата
        text: Текст сообщения (для текстовых сообщений)
        photo_file_id: ID фото в Telegram
        document_file_id: ID документа в Telegram
        caption: Подпись к медиа
        pin: Закрепить сообщение после отправки
        notify: Отправлять уведомление участникам
        delete_after_days: Удалить сообщение через N дней (1-3)
    
    Returns:
        ID отправленного сообщения или None при ошибке
    """
    try:
        bot = get_bot()
        message = None
        
        # Определяем тип сообщения
        if photo_file_id:
            logger.info(f"📤 Отправка фото в чат {chat_id}")
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_file_id,
                caption=escape_markdown_v2(caption) if caption else None,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_notification=not notify
            )
        elif document_file_id:
            logger.info(f"📤 Отправка документа в чат {chat_id}")
            message = await bot.send_document(
                chat_id=chat_id,
                document=document_file_id,
                caption=escape_markdown_v2(caption) if caption else None,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_notification=not notify
            )
        else:
            logger.info(f"📤 Отправка текстового сообщения в чат {chat_id}")
            message = await bot.send_message(
                chat_id=chat_id,
                text=escape_markdown_v2(text) if text else "⚠️ Пустое сообщение",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_notification=not notify
            )
        
        # Закрепляем сообщение если нужно
        if pin and message:
            try:
                await bot.pin_chat_message(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    disable_notification=True
                )
                logger.info(f"📌 Сообщение {message.message_id} закреплено в чате {chat_id}")
            except (BadRequest, Forbidden) as e:
                logger.warning(f"⚠️ Не удалось закрепить сообщение в чате {chat_id}: {e}")
        
        # Планируем удаление если нужно
        if delete_after_days and message:
            asyncio.create_task(
                schedule_deletion(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    delay_days=delete_after_days
                )
            )
            logger.info(f"⏳ Удаление сообщения {message.message_id} запланировано через {delete_after_days} дн.")
        
        if message:
            logger.info(f"✅ Сообщение успешно отправлено в чат {chat_id}, ID: {message.message_id}")
            return message.message_id
        
        return None
        
    except (BadRequest, Forbidden) as e:
        logger.error(f"❌ Ошибка отправки в чат {chat_id}: {e}. Деактивируем задачу.")
        # Деактивируем задачу если чат недоступен
        # (вызовется из publish_and_reschedule)
        raise
    except TelegramError as e:
        logger.error(f"❌ Ошибка Telegram API при отправке: {e}")
        return None
    except Exception as e:
        logger.exception(f"❌ Неожиданная ошибка при отправке сообщения: {e}")
        return None

async def publish_and_reschedule(
    msg_id: int,
    chat_id: int,
    text: Optional[str],
    photo_file_id: Optional[str],
    document_file_id: Optional[str],
    caption: Optional[str],
    recurrence: str,
    pin: bool,
    notify: bool,
    delete_after_days: Optional[int],
    original_publish_at: str
):
    """
    Публикует сообщение и перепланирует следующее выполнение для повторяющихся задач.
    
    Args:
        msg_id: ID задачи в базе данных
        chat_id: ID чата
        text: Текст сообщения
        photo_file_id: ID фото
        document_file_id: ID документа
        caption: Подпись к медиа
        recurrence: Периодичность ('once', 'daily', 'weekly', 'monthly')
        pin: Закреплять сообщение
        notify: Отправлять уведомления
        delete_after_days: Удалять через N дней
        original_publish_at: Оригинальное время первой публикации
    """
    logger.info(f"🔄 Запуск задачи {msg_id} для чата {chat_id}")
    
    try:
        # Публикуем сообщение
        msg_id_telegram = await publish_message(
            chat_id=chat_id,
            text=text,
            photo_file_id=photo_file_id,
            document_file_id=document_file_id,
            caption=caption,
            pin=pin,
            notify=notify,
            delete_after_days=delete_after_days
        )
        
        # Если сообщение не отправлено - выходим
        if msg_id_telegram is None:
            logger.warning(f"⚠️ Сообщение для задачи {msg_id} не отправлено. Пропускаем перепланирование.")
            return
        
        # Обрабатываем повторяющиеся задачи
        if recurrence != 'once':
            # Получаем данные о задаче для расчёта следующего времени
            task = get_message_by_id(msg_id)
            if not task:
                logger.error(f"❌ Задача {msg_id} не найдена в БД для перепланирования")
                return
            
            # Конвертируем текущее время публикации в datetime
            try:
                last_publish_time = datetime.datetime.fromisoformat(task['publish_at'])
            except (TypeError, ValueError) as e:
                logger.error(f"❌ Ошибка парсинга publish_at для задачи {msg_id}: {e}")
                return
            
            # Рассчитываем следующее время публикации
            next_time = next_recurrence_time(
                original=datetime.datetime.fromisoformat(original_publish_at),
                recurrence=recurrence,
                last=last_publish_time
            )
            
            if next_time:
                # Проверяем максимальный срок (365 дней)
                max_end_date = datetime.datetime.fromisoformat(task['max_end_date'])
                if next_time > max_end_date:
                    logger.info(f"⏹️ Задача {msg_id} достигла максимального срока. Деактивируем.")
                    deactivate_message(msg_id)
                    return
                
                # Обновляем время следующей публикации
                next_time_iso = next_time.isoformat()
                success = update_next_publish_time(msg_id, next_time_iso)
                
                if success:
                    logger.info(
                        f"⏰ Задача {msg_id} перепланирована на {next_time_iso} "
                        f"(следующая публикация через {(next_time - datetime.datetime.utcnow()).total_seconds() / 3600:.1f} часов)"
                    )
                else:
                    logger.error(f"❌ Не удалось обновить время для задачи {msg_id}")
            else:
                logger.info(f"⏹️ Цикл повторений для задачи {msg_id} завершён")
                deactivate_message(msg_id)
        
        # Для одноразовых задач деактивируем после отправки
        elif recurrence == 'once':
            logger.info(f"⏹️ Одноразовая задача {msg_id} выполнена. Деактивируем.")
            deactivate_message(msg_id)
    
    except (BadRequest, Forbidden) as e:
        # Обрабатываем ошибки, связанные с недоступностью чата
        logger.error(f"❌ Критическая ошибка для чата {chat_id}: {e}. Деактивируем все задачи для этого чата.")
        deactivate_chat_tasks(chat_id)
    except Exception as e:
        logger.exception(f"❌ Неожиданная ошибка в publish_and_reschedule для задачи {msg_id}: {e}")

async def schedule_deletion(chat_id: int, message_id: int, delay_days: int):
    """
    Планирует удаление сообщения через указанное количество дней.
    
    Args:
        chat_id: ID чата
        message_id: ID сообщения
        delay_days: Количество дней до удаления
    """
    if delay_days not in (1, 2, 3):
        logger.warning(f"⚠️ Некорректное значение delete_after_days={delay_days}. Используется 1 день.")
        delay_days = 1
    
    delay_seconds = delay_days * 24 * 3600
    logger.info(f"⏳ Ожидание {delay_days} дней ({delay_seconds} сек) перед удалением сообщения {message_id} в чате {chat_id}")
    
    await asyncio.sleep(delay_seconds)
    
    try:
        bot = get_bot()
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"🗑️ Сообщение {message_id} удалено из чата {chat_id} (спустя {delay_days} дн.)")
    except (BadRequest, Forbidden) as e:
        logger.warning(f"⚠️ Не удалось удалить сообщение {message_id} в чате {chat_id}: {e}")
    except Exception as e:
        logger.exception(f"❌ Ошибка при удалении сообщения {message_id}: {e}")

def deactivate_chat_tasks(chat_id: int):
    """
    Деактивирует все активные задачи для указанного чата.
    
    Args:
        chat_id: ID чата
    """
    try:
        tasks = get_all_active_messages()
        deactivated_count = 0
        
        for task in tasks:
            if task['chat_id'] == chat_id:
                deactivate_message(task['id'])
                deactivated_count += 1
                logger.info(f"⏹️ Деактивирована задача {task['id']} для недоступного чата {chat_id}")
        
        if deactivated_count > 0:
            logger.warning(f"⏹️ Всего деактивировано {deactivated_count} задач для чата {chat_id}")
    
    except Exception as e:
        logger.exception(f"❌ Ошибка при деактивации задач для чата {chat_id}: {e}")

async def health_check() -> dict:
    """
    Проверяет здоровье планировщика задач.
    
    Returns:
        Словарь с информацией о состоянии
    """
    try:
        tasks = get_all_active_messages()
        now = datetime.datetime.utcnow()
        
        # Статистика по типам повторений
        recurrence_stats = {'once': 0, 'daily': 0, 'weekly': 0, 'monthly': 0}
        for task in tasks:
            recurrence_stats[task['recurrence']] += 1
        
        # Задачи, которые должны были выполниться в прошлом
        overdue_tasks = [
            task for task in tasks
            if datetime.datetime.fromisoformat(task['publish_at']) < now
        ]
        
        return {
            "status": "ok",
            "active_tasks_count": len(tasks),
            "recurrence_stats": recurrence_stats,
            "overdue_tasks_count": len(overdue_tasks),
            "next_tasks": [
                {
                    "id": task['id'],
                    "chat_id": task['chat_id'],
                    "publish_at": task['publish_at'],
                    "recurrence": task['recurrence']
                }
                for task in sorted(tasks, key=lambda x: x['publish_at'])[:5]
            ]
        }
    except Exception as e:
        logger.error(f"❌ Ошибка проверки здоровья планировщика: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

async def test_chat_access(chat_id: int) -> Tuple[bool, str]:
    """
    Проверяет доступ к чату и права бота.
    
    Args:
        chat_id: ID чата для проверки
        
    Returns:
        (успешно, сообщение об ошибке)
    """
    try:
        bot = get_bot()
        chat = await bot.get_chat(chat_id)
        
        # Проверяем, является ли бот администратором
        member = await bot.get_chat_member(chat_id, bot.id)
        if not member.status == 'administrator':
            return False, "Бот не является администратором чата"
        
        # Проверяем, может ли бот закреплять сообщения
        if member.can_pin_messages is False:
            logger.warning(f"⚠️ Бот не имеет прав на закрепление сообщений в чате {chat_id}")
        
        logger.info(f"✅ Доступ к чату {chat_id} подтверждён. Название: {chat.title}")
        return True, ""
    
    except (BadRequest, Forbidden) as e:
        error_msg = str(e)
        if "bot was kicked" in error_msg.lower():
            return False, "Бот был удалён из чата"
        elif "chat not found" in error_msg.lower():
            return False, "Чат не найден"
        elif "not enough rights" in error_msg.lower():
            return False, "Недостаточно прав для управления чатом"
        return False, f"Ошибка доступа к чату: {error_msg}"
    except Exception as e:
        logger.exception(f"❌ Неожиданная ошибка при проверке чата {chat_id}: {e}")
        return False, f"Внутренняя ошибка: {str(e)}"

async def publish_test_message(chat_id: int) -> bool:
    """
    Публикует тестовое сообщение для проверки работоспособности.
    
    Args:
        chat_id: ID чата
        
    Returns:
        True если сообщение отправлено успешно
    """
    try:
        bot = get_bot()
        message = await bot.send_message(
            chat_id=chat_id,
            text="✅ Тестовое сообщение от планировщика задач\n\n"
                 "Всё работает корректно! Вы можете удалять это сообщение.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        logger.info(f"✅ Тестовое сообщение отправлено в чат {chat_id}, ID: {message.message_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки тестового сообщения в чат {chat_id}: {e}")
        return False
