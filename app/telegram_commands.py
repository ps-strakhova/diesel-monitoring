from __future__ import annotations
import threading
import time
import requests
from typing import Optional
from .command_processor import CommandProcessor

BOT_USERNAME = "@diesel_monitor_bot"


class TelegramCommandListener:
    def __init__(self, token: str, db_path: str, allowed_chat_id: Optional[str] = None, monitor=None):
        self.token = token
        # ✅ СОЗДАЕМ ЭКЗЕМПЛЯР CommandProcessor
        self.cmd_proc = CommandProcessor(db_path, monitor)
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id is not None else None
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._pending: dict[str, str] = {}

    def queue_reply_for_code(self, code: str, text: str) -> None:
        """Сохраняет ответ для автоматической отправки по коду"""
        self._pending[str(code)] = str(text)

    def start(self) -> None:
        if not self.token:
            print("⚠️ Telegram токен не настроен, команды не работают")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("✅ Telegram Command Listener запущен")

    def stop(self) -> None:
        self._stop = True

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _send(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
        """Отправка сообщения"""
        payload = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        try:
            response = requests.post(self._api("sendMessage"), json=payload, timeout=30)
            if response.status_code != 200:
                print(f"❌ Ошибка отправки в Telegram: {response.text}")
        except Exception as e:
            print(f"❌ Ошибка отправки в Telegram: {e}")

    def _run(self) -> None:
        """Основной цикл получения обновлений"""
        print("🔄 Telegram Command Listener: начал прослушивание")
        while not self._stop:
            try:
                # Получаем обновления
                response = requests.get(
                    self._api("getUpdates"),
                    params={"timeout": 30, "offset": self._offset},
                    timeout=45,
                )
                data = response.json()

                if not data.get("ok"):
                    print(f"⚠️ Telegram API ошибка: {data}")
                    time.sleep(2)
                    continue

                # Обрабатываем каждое обновление
                for upd in data.get("result", []):
                    self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                    self._process_update(upd)

            except requests.exceptions.Timeout:
                # Таймаут - нормальная ситуация, продолжаем
                pass
            except Exception as e:
                time.sleep(2)

    def _process_update(self, upd: dict) -> None:
        """Обработка одного обновления"""
        msg = upd.get("message")
        if not msg:
            return

        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None:
            return

        # Проверяем разрешенный чат
        if self.allowed_chat_id is not None and str(chat_id) != self.allowed_chat_id:
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        msg_id = msg.get("message_id")
        user = msg.get("from", {}) or {}
        username = user.get("username") or user.get("first_name") or "unknown"

        # ✅ 1. Проверяем авто-ответ по code (для реплаев на уведомления)
        code = None
        if "code=" in text:
            try:
                code = text.split("code=", 1)[1].strip().split()[0]
            except Exception:
                pass

        if code and code in self._pending:
            ans = self._pending.pop(code)
            print(f"↩️ Авто-ответ по code={code}")
            self._send(int(chat_id), ans, reply_to_message_id=msg_id)
            return

        # ✅ 2. Проверяем, является ли сообщение командой
        #    В групповых чатах команда должна начинаться с / или содержать @username
        is_command = text.startswith("/")
        is_mention = BOT_USERNAME.lower() in text.lower()

        # Для групповых чатов: если не команда и не упоминание - игнорируем
        if not is_command and not is_mention:
            return

        # Очищаем текст от упоминания бота
        clean_text = text.replace(BOT_USERNAME, "").strip()
        if not clean_text and not is_command:
            return

        # ✅ 3. Обрабатываем команду через CommandProcessor
        try:
            response = self.cmd_proc.process_command(clean_text, username, str(chat_id))
            if response is not None:
                self._send(int(chat_id), response, reply_to_message_id=msg_id)
            else:
                # Если команда не распознана, но это была команда - отвечаем
                if is_command:
                    self._send(int(chat_id), "❌ Неизвестная команда. Используйте /help", reply_to_message_id=msg_id)
        except Exception as e:
            print(f"❌ Ошибка обработки команды: {e}")
            self._send(int(chat_id), f"❌ Ошибка: {e}", reply_to_message_id=msg_id)