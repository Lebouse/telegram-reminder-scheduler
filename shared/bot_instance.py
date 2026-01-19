from telegram import Bot
from config import BOT_TOKEN

_bot_instance = None

def get_bot() -> Bot:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = Bot(token=BOT_TOKEN)
    return _bot_instance
