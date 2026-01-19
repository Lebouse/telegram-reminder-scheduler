# web_api.py

import asyncio
import os
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

from config import WEB_API_SECRET, BOT_TOKEN
from scheduler_logic import publish_message

app = FastAPI(title="Telegram Scheduler API")

class PublishRequest(BaseModel):
    chat_id: int
    text: Optional[str] = None
    photo_file_id: Optional[str] = None
    document_file_id: Optional[str] = None
    caption: Optional[str] = None
    pin: bool = False
    notify: bool = True
    delete_after_days: Optional[int] = None

@app.post("/publish")
async def web_publish(request: PublishRequest, x_secret: str = Header(...)):
    if WEB_API_SECRET and x_secret != WEB_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    try:
        msg_id = await publish_message(
            chat_id=request.chat_id,
            text=request.text,
            photo_file_id=request.photo_file_id,
            document_file_id=request.document_file_id,
            caption=request.caption,
            pin=request.pin,
            notify=request.notify,
            delete_after_days=request.delete_after_days
        )
        if msg_id is None:
            raise HTTPException(status_code=500, detail="Failed to send message")
        return {"ok": True, "message_id": msg_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
