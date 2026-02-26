from __future__ import annotations

import json
import os
import urllib.request


def send_telegram(message: str) -> bool:
    """
    Telegram Bot API로 메시지 전송 (fire-and-forget).
    TG_BOT_TOKEN / TG_ALLOWED_CHAT_ID 환경변수 필요.
    실패 시 False 반환 — 예외 전파 없음.
    """
    token = os.getenv("TG_BOT_TOKEN", "")
    chat_id = os.getenv("TG_ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False
