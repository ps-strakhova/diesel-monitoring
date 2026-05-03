import requests
import re


class TelegramNotifier:
    def __init__(self, bot_token: str, notify_chat_id: str, reply_chat_id: str = None):
        self.bot_token = bot_token
        self.notify_chat_id = notify_chat_id  # Канал для уведомлений
        self.reply_chat_id = reply_chat_id or notify_chat_id  # Группа для ответов
        self._message_ids: dict[str, int] = {}

    def enabled(self) -> bool:
        return bool(self.bot_token and self.notify_chat_id)

    def send(self, text: str) -> int | None:
        """Отправка уведомления в КАНАЛ"""
        if not self.enabled():
            return None

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": self.notify_chat_id, "text": text}, timeout=10)
            if r.status_code == 200:
                result = r.json()
                message_id = result.get("result", {}).get("message_id")
                code = self._extract_code(text)
                if code and message_id:
                    self._message_ids[code] = message_id
                return message_id
            return None
        except Exception as e:
            print("TG exception:", repr(e))
            return None

    def send_reply_by_code(self, text: str, code: str) -> bool:
        """Отправка ответа в ГРУППУ ОБСУЖДЕНИЯ"""
        if not self.enabled():
            return False

        message_id = self._message_ids.get(code)
        if not message_id:
            print(f"No message_id found for code {code}, sending as normal")
            return self.send(text) is not None

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            payload = {
                "chat_id": self.reply_chat_id,  # <-- Используем reply_chat_id (группа обсуждения)
                "text": text,
                "reply_to_message_id": message_id,
                "allow_sending_without_reply": True
            }
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                print(f"Reply sent to discussion group {self.reply_chat_id}")
            return r.status_code == 200
        except Exception as e:
            print("TG reply exception:", repr(e))
            return False

    def _extract_code(self, text: str) -> str | None:
        match = re.search(r'code=([A-Z_0-9]+)', text)
        return match.group(1) if match else None