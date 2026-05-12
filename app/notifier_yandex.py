#notifier_yandex.py
import requests
import time
import re
from threading import Lock
from typing import Optional

class YandexNotifier:
    def __init__(self, api_key: str, chat_id: str):
        self.api_key = api_key
        self.chat_id = chat_id
        self._last_send_ts = 0
        self._lock = Lock()
        self._message_ids: dict[str, str] = {}      # code -> message_id
        self._code_by_msg_id: dict[str, str] = {}   # message_id -> code

    def enabled(self) -> bool:
        return bool(self.api_key and self.chat_id)

    def send(self, message: str, reply_to_message_id: Optional[str] = None, chat_id: Optional[str] = None, skip_rate_limit: bool = False) -> Optional[str]:
        if not self.enabled():
            return None
        if not skip_rate_limit:
            with self._lock:
                now = time.time()
                if now - self._last_send_ts < 1:
                    return None
                self._last_send_ts = now

        target_chat = chat_id if chat_id else self.chat_id
        payload = {
            "chat_id": target_chat,
            "text": message
        }
        if reply_to_message_id:
            payload["reply_message_id"] = reply_to_message_id

        try:
            response = requests.post(
                "https://botapi.messenger.yandex.net/bot/v1/messages/sendText/",
                headers={
                    "Authorization": f"OAuth {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=10
            )
            if response.status_code == 200:
                result = response.json()
                message_id = result.get("message_id")
                code = self._extract_code(message)
                if code and message_id:
                    self._message_ids[code] = message_id
                    self._code_by_msg_id[message_id] = code
                return message_id
            return None
        except Exception as e:
            print(f"Yandex send error: {e}")
            return None

    def send_reply(self, message: str, reply_to_message_id: str, chat_id: Optional[str] = None) -> Optional[str]:
        """Отправить сообщение как ответ (reply) на конкретное сообщение"""
        return self.send(message, reply_to_message_id=reply_to_message_id, chat_id=chat_id, skip_rate_limit=True)

    def send_reply_by_code(self, message: str, code: str) -> bool:
        """Автоматический ответ на ранее отправленное уведомление с данным code"""
        message_id = self._message_ids.get(code)
        if not message_id:
            print(f"No Yandex message_id found for code {code}, cannot auto-reply")
            return False
        return self.send_reply(message, message_id) is not None

    def get_code_by_message_id(self, message_id: str) -> Optional[str]:
        """Получить code по message_id (для обработки реплаев от пользователя)"""
        return self._code_by_msg_id.get(message_id)

    def _extract_code(self, text: str) -> Optional[str]:
        match = re.search(r'code=([A-Z_0-9]+)', text)
        return match.group(1) if match else None