# app/telegram_commands.py
from __future__ import annotations

import time
import threading
import sqlite3
import requests
from typing import Optional


BOT_USERNAME = "@diesel_monitor_bot"

# ✅ используем единый рендерер
from .status_renderer import render_status_text


class TelegramCommandListener:
    """
    Команды (в группе обсуждений / личке):
      /status
      /last [N]
      /help CODE
      /explain [CODE]
      /ack [note]

    ВАЖНО:
    - Команды НЕ работают в канале.
    - В discussion group ответы делаем reply на сообщение пользователя,
      чтобы они отображались в ветке комментариев к посту канала.
    """

    def __init__(self, token: str, db_path: str, allowed_chat_id: Optional[str] = None, monitor=None):
        self.token = token
        self.db_path = db_path
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id is not None else None
        self.monitor = monitor
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0

        # Для авто-reply по code (если ты уже добавляла queue_reply_for_code — оставь как есть в твоей версии)
        self._pending: dict[str, str] = {}

    def queue_reply_for_code(self, code: str, text: str) -> None:
        self._pending[str(code)] = str(text)

    def start(self) -> None:
        if not self.token:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _send(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        try:
            requests.post(self._api("sendMessage"), json=payload, timeout=30)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop:
            try:
                r = requests.get(
                    self._api("getUpdates"),
                    params={"timeout": 30, "offset": self._offset},
                    timeout=45,
                )
                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue

                for upd in data.get("result", []):
                    self._offset = max(self._offset, upd.get("update_id", 0) + 1)

                    msg = upd.get("message")
                    if not msg:
                        continue

                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    if chat_id is None:
                        continue

                    if self.allowed_chat_id is not None and str(chat_id) != self.allowed_chat_id:
                        continue

                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue

                    msg_id = msg.get("message_id")

                    # ✅ авто-ответ по code=...
                    # если сообщение содержит "code=XXX" и у нас есть pending — делаем reply
                    code = None
                    if "code=" in text:
                        try:
                            code = text.split("code=", 1)[1].strip().split()[0]
                        except Exception:
                            code = None

                    if code and code in self._pending:
                        ans = self._pending.pop(code)
                        self._send(int(chat_id), ans, reply_to_message_id=msg_id)
                        continue

                    # если не команда — отвечаем только при упоминании бота
                    if not text.startswith("/") and (BOT_USERNAME.lower() not in text.lower()):
                        continue

                    user = msg.get("from", {}) or {}
                    username = user.get("username") or user.get("first_name") or "unknown"

                    answer = self._handle(text=text, chat_id=chat_id, username=username)
                    if answer:
                        self._send(int(chat_id), answer, reply_to_message_id=msg_id)

            except Exception:
                time.sleep(2)

    def _handle(self, text: str, chat_id: int, username: str) -> str:
        cleaned = text.replace(BOT_USERNAME, "").strip()
        parts = cleaned.split()
        cmd = parts[0].lower() if parts else ""

        if cmd == "/status":
            return render_status_text(self.db_path)

        if cmd == "/last":
            n = 10
            if len(parts) >= 2:
                try:
                    n = max(1, min(50, int(parts[1])))
                except Exception:
                    n = 10
            return self._cmd_last(n)

        if cmd == "/help":
            if len(parts) < 2:
                return "Использование: /help CODE\nНапример: /help LOW_OIL_PRESSURE"
            code = parts[1].strip().upper()
            return self._cmd_help(code)

        if cmd == "/explain":
            code = parts[1].strip().upper() if len(parts) >= 2 else None
            return self._cmd_explain(code)

        if cmd == "/ack":
            note = " ".join(parts[1:]) if len(parts) >= 2 else ""
            return self._cmd_ack(chat_id=str(chat_id), username=username, note=note)

        return "Команды: /status, /last [N], /help CODE, /explain [CODE], /ack [note]"

    def _cmd_last(self, n: int) -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        rows = c.execute(
            "select ts, severity, title, coalesce(code,''), coalesce(details,'') from events order by id desc limit ?",
            (n,),
        ).fetchall()
        conn.close()

        if not rows:
            return "Событий пока нет."

        lines = ["🧾 Последние события:"]
        for ts, sev, title, code, details in rows:
            lines.append(f"- [{sev}] {title} ({code})")
        return "\n".join(lines)

    def _cmd_help(self, code: str) -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        row = c.execute(
            "select short_title, description, recommendations from knowledge_base where code=?",
            (code,),
        ).fetchone()
        conn.close()

        if not row:
            return f"Нет статьи для кода {code}."

        title, desc, rec = row[0] or "", row[1] or "", row[2] or ""
        txt = f"📘 {code}\n{title}"
        if desc:
            txt += f"\n\n{desc}"
        if rec:
            txt += f"\n\nЧто делать:\n{rec}"
        return txt

    def _cmd_explain(self, code: Optional[str]) -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        if code is None:
            row = c.execute("select coalesce(code,'') from events order by id desc limit 1").fetchone()
            code = (row[0] or "").strip() if row else ""
            code = code or None

        if not code:
            conn.close()
            return "Нет событий с code для объяснения. Укажи CODE: /explain LOW_OIL_PRESSURE"

        row = c.execute(
            "select short_title, description, recommendations from knowledge_base where code=?",
            (code,),
        ).fetchone()
        conn.close()

        if not row:
            return f"Нет статьи для кода {code}."

        title, desc, rec = row[0] or "", row[1] or "", row[2] or ""
        txt = f"📘 {code}\n{title}"
        if desc:
            txt += f"\n\n{desc}"
        if rec:
            txt += f"\n\nЧто делать:\n{rec}"
        return txt

    def _cmd_ack(self, chat_id: str, username: str, note: str = "") -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        row = c.execute(
            "select id, coalesce(code,''), severity, title from events order by id desc limit 1"
        ).fetchone()
        if not row:
            conn.close()
            return "Событий пока нет."

        event_id, code, severity, title = row
        ts = int(time.time())

        c.execute(
            """
            insert into acknowledgements(event_id, code, ack_by, ack_chat_id, ack_ts, note)
            values (?, ?, ?, ?, ?, ?)
            """,
            (event_id, code, username, chat_id, ts, note),
        )
        conn.commit()
        conn.close()

        extra = f" | note: {note}" if note else ""
        return f"✅ ACK: [{severity}] {title} ({code}){extra}"
