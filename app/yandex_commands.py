# app/yandex_commands.py
from __future__ import annotations

import sqlite3
import re
from typing import Optional

from .status_renderer import render_status_text


class YandexCommandHandler:
    """
    Обработка команд для Яндекс‑мессенджера (через вебхук).
    Не использует long polling, только синхронную обработку одного сообщения.
    """

    def __init__(self, db_path: str, monitor=None):
        self.db_path = db_path
        self.monitor = monitor  # нужен для доступа к ya, tg, unit_name и т.д.

    def handle_command(self, text: str, user_id: str) -> Optional[str]:
        """
        Основной метод: принимает текст команды и user_id,
        возвращает строку для ответа или None, если отвечать не надо.
        """
        if not text.startswith('/'):
            # Не команда – можно предложить список команд
            return self._help_general()

        cleaned = text.strip()
        parts = cleaned.split()
        cmd = parts[0].lower()

        if cmd == "/status":
            return render_status_text(self.db_path)

        if cmd == "/last":
            n = 10
            if len(parts) >= 2:
                try:
                    n = max(1, min(50, int(parts[1])))
                except ValueError:
                    pass
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
            return self._cmd_ack(user_id=user_id, note=note)

        return self._help_general()

    # ------------------------------------------------------------
    # Вспомогательные методы (аналоги TelegramCommandListener)
    # ------------------------------------------------------------
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

    def _cmd_ack(self, user_id: str, note: str = "") -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        row = c.execute(
            "select id, coalesce(code,''), severity, title from events order by id desc limit 1"
        ).fetchone()
        if not row:
            conn.close()
            return "Событий пока нет."

        event_id, code, severity, title = row
        ts = int(__import__('time').time())

        c.execute(
            """
            insert into acknowledgements(event_id, code, ack_by, ack_chat_id, ack_ts, note)
            values (?, ?, ?, ?, ?, ?)
            """,
            (event_id, code, user_id, user_id, ts, note),
        )
        conn.commit()
        conn.close()

        extra = f" | note: {note}" if note else ""
        return f"✅ ACK: [{severity}] {title} ({code}){extra}"

    def _help_general(self) -> str:
        return (
            "📟 Доступные команды:\n"
            "/status – полный статус установки\n"
            "/last [N] – последние N событий (по умолч. 10)\n"
            "/help CODE – справка по коду (например /help LOW_OIL_PRESSURE)\n"
            "/explain [CODE] – объяснить последнее или указанное событие\n"
            "/ack [note] – подтвердить последнее событие (можно с заметкой)"
        )