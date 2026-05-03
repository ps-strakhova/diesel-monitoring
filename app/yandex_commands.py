from __future__ import annotations
import json
import asyncio
from typing import Optional
from .status_renderer import render_status_text, render_status_compact
from .storage import get_last_event_code, get_kb_article, ack_last_event


class YandexCommandHandler:
    """Обработчик команд для Яндекс.Мессенджера"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def handle_command(self, message_text: str, user_id: str) -> str:
        """Обработка входящей команды"""
        text = message_text.strip().lower()

        # /status - полный статус
        if text == "/status" or text == "статус":
            return render_status_text(self.db_path)

        # /compact - краткий статус
        if text == "/compact" or text == "кратко":
            return render_status_compact(self.db_path)

        # /last - последние события
        if text.startswith("/last") or text.startswith("последние"):
            parts = text.split()
            n = 10
            if len(parts) >= 2:
                try:
                    n = max(1, min(50, int(parts[1])))
                except:
                    pass
            return self._get_last_events(n)

        # /help CODE - справка по коду
        if text.startswith("/help") or text.startswith("помощь"):
            parts = text.split()
            if len(parts) >= 2:
                return self._get_help(parts[1].upper())
            return "Использование: /help CODE\nНапример: /help LOW_OIL_PRESSURE"

        # /ack - подтверждение события
        if text.startswith("/ack") or text.startswith("подтвердить"):
            note = text.replace("/ack", "").replace("подтвердить", "").strip()
            return self._ack_event(user_id, note)

        # /explain - объяснение последнего события
        if text == "/explain" or text == "объясни":
            return self._explain_last()

        return self._get_help_menu()

    def _get_last_events(self, n: int) -> str:
        """Получение последних N событий"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        rows = c.execute(
            "SELECT ts, severity, title, coalesce(code,'') FROM events "
            "ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()

        if not rows:
            return "Событий пока нет."

        lines = ["📋 Последние события:"]
        for ts, sev, title, code in rows:
            from datetime import datetime
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            icon = "🚨" if sev == "alarm" else ("⚠️" if sev == "warn" else "ℹ️")
            lines.append(f"{icon} {time_str} - {title} ({code})")

        return "\n".join(lines[:n])

    def _get_help(self, code: str) -> str:
        """Получение справки по коду"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        row = c.execute(
            "SELECT short_title, description, recommendations FROM knowledge_base WHERE code=?",
            (code,)
        ).fetchone()
        conn.close()

        if not row:
            return f"❌ Нет статьи для кода {code}"

        title, desc, rec = row[0] or "", row[1] or "", row[2] or ""
        result = f"📘 {code}\n{title}"
        if desc:
            result += f"\n\n{desc}"
        if rec:
            result += f"\n\n💡 Что делать:\n{rec}"
        return result

    def _ack_event(self, user_id: str, note: str) -> str:
        """Подтверждение последнего события"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        ok, msg = ack_last_event(conn, user_id, f"yandex_{user_id}", note)
        conn.close()
        return msg

    def _explain_last(self) -> str:
        """Объяснение последнего события"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        code = get_last_event_code(conn)
        conn.close()

        if not code:
            return "Нет событий для объяснения"

        return self._get_help(code)

    def _get_help_menu(self) -> str:
        """Меню помощи"""
        return """🤖 Доступные команды:
/status - полный статус установки
/compact - краткий статус
/last [N] - последние N событий
/help CODE - справка по коду
/explain - объяснить последнее событие
/ack [note] - подтвердить последнее событие
"""