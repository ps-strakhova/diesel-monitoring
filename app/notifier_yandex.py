import requests
import time
import re
from threading import Lock


class YandexNotifier:
    def __init__(self, api_key: str, chat_id: str):
        self.api_key = api_key
        self.chat_id = chat_id
        self._last_send_ts = 0
        self._lock = Lock()
        self._message_ids: dict[str, str] = {}

    def enabled(self) -> bool:
        """Проверка, настроен ли Яндекс"""
        return bool(self.api_key and self.chat_id)

    def send(self, message: str) -> str | None:
        if not self.enabled():
            return None

        with self._lock:
            now = time.time()
            if now - self._last_send_ts < 1:
                return None
            self._last_send_ts = now

        try:
            response = requests.post(
                "https://botapi.messenger.yandex.net/bot/v1/messages/sendText/",
                headers={
                    "Authorization": f"OAuth {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "chat_id": self.chat_id,
                    "text": message
                },
                timeout=10
            )
            if response.status_code == 200:
                result = response.json()
                message_id = result.get("message_id")
                code = self._extract_code(message)
                if code and message_id:
                    self._message_ids[code] = message_id
                return message_id
            return None
        except Exception as e:
            print(f"Yandex send error: {e}")
            return None

    def send_reply_by_code(self, message: str, code: str) -> bool:
        """Отправляет ответ на сообщение с указанным code"""
        if not self.enabled():
            return False

        message_id = self._message_ids.get(code)
        if not message_id:
            print(f"No Yandex message_id found for code {code}, sending as normal")
            return self.send(message) is not None

        try:
            response = requests.post(
                "https://botapi.messenger.yandex.net/bot/v1/messages/sendText/",
                headers={
                    "Authorization": f"OAuth {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "reply_message_id": message_id
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            print(f"Yandex reply error: {e}")
            return False

    def _extract_code(self, text: str) -> str | None:
        match = re.search(r'code=([A-Z_0-9]+)', text)
        return match.group(1) if match else None