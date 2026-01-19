# shared/models.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class ScheduledMessage:
    id: int
    chat_id: int
    text: Optional[str]
    photo_file_id: Optional[str]
    document_file_id: Optional[str]
    caption: Optional[str]
    publish_at: str  # ISO format UTC
    original_publish_at: str
    recurrence: str  # 'once', 'daily', 'weekly', 'monthly'
    pin: bool
    notify: bool
    delete_after_days: Optional[int]
    active: bool
