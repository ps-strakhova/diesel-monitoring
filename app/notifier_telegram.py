# app/notifier_telegram.py
"""
Уведомления через Telegram
"""

import requests
import re
from typing import Optional


class TelegramNotifier:
    def __init__(self, bot_token: str, notify_chat_id: str, reply_chat_id: str = None):
        self.bot_token = bot_token
        self.notify_chat_id = notify_chat_id  # Канал для уведомлений
        self.reply_chat_id = reply_chat_id or notify_chat_id  # Группа для ответов
        self._message_ids: dict[str, int] = {}

    def enabled(self) -> bool:
        return bool(self.bot_token and self.notify_chat_id)

    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def _extract_code(self, text: str) -> Optional[str]:
        match = re.search(r'code=([A-Z_0-9]+)', text)
        return match.group(1) if match else None

    def _send_request(self, chat_id: str, text: str, reply_to_id: Optional[int] = None) -> Optional[int]:
        """Базовая отправка сообщения"""
        if not self.enabled():
            return None

        payload = {"chat_id": chat_id, "text": text}
        if reply_to_id is not None:
            payload["reply_to_message_id"] = reply_to_id
            payload["allow_sending_without_reply"] = True

        try:
            r = requests.post(self._api_url(), json=payload, timeout=10)
            if r.status_code == 200:
                result = r.json()
                return result.get("result", {}).get("message_id")
            return None
        except Exception as e:
            print("TG exception:", repr(e))
            return None

    def send(self, text: str) -> Optional[int]:
        """Отправка уведомления в КАНАЛ"""
        message_id = self._send_request(self.notify_chat_id, text)

        if message_id:
            code = self._extract_code(text)
            if code:
                self._message_ids[code] = message_id

        return message_id

    def send_reply_by_code(self, text: str, code: str) -> bool:
        """Отправка ответа в ГРУППУ ОБСУЖДЕНИЯ по коду"""
        message_id = self._message_ids.get(code)

        if not message_id:
            print(f"No message_id found for code {code}, sending as normal")
            return self.send(text) is not None

        result = self._send_request(self.reply_chat_id, text, reply_to_id=message_id)
        return result is not None