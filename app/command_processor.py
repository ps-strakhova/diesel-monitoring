import sqlite3
from typing import Optional
from .status_renderer import render_status_text

try:
    from llm_agent.simple_agent import DieselLLMAgent
except Exception:
    DieselLLMAgent = None


class CommandProcessor:
    def __init__(self, db_path: str, monitor=None):
        self.db_path = db_path
        self.monitor = monitor
        self.llm_agent = None
        if DieselLLMAgent:
            try:
                self.llm_agent = DieselLLMAgent(auto_index=False)
            except Exception as e:
                print(f"⚠️ LLM агент не инициализирован: {e}")

    def get_unit_name(self) -> str:
        """Возвращает имя установки"""
        if self.monitor and hasattr(self.monitor, "unit_name"):
            try:
                return self.monitor.unit_name()
            except Exception:
                pass

        try:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                row = c.execute("""
                    SELECT last_value_text
                    FROM last_value lv
                    JOIN register_catalog rc ON rc.id = lv.catalog_id
                    WHERE rc.addr = 3000
                    LIMIT 1
                """).fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
        except Exception:
            pass
        return "Установка"

    def cmd_status(self, compact: bool = False) -> str:
        """Полный или компактный статус"""
        if compact and self.monitor and hasattr(self.monitor, "get_compact_status_from_memory"):
            try:
                return self.monitor.get_compact_status_from_memory()
            except Exception as e:
                return f"❌ Ошибка получения статуса: {e}"
        try:
            return render_status_text(self.db_path)
        except Exception as e:
            return f"❌ Ошибка рендеринга статуса: {e}"

    def cmd_last(self, n: int = 10) -> str:
        """Последние события"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                rows = c.execute("""
                    SELECT ts, severity, title, COALESCE(code, ''), COALESCE(details, '')
                    FROM events
                    ORDER BY id DESC
                    LIMIT ?
                """, (n,)).fetchall()

            if not rows:
                return "📭 Событий пока нет."

            lines = ["🧾 Последние события:"]
            for ts, sev, title, code, details in rows:
                code_part = f" ({code})" if code else ""
                lines.append(f"• [{sev}] {title}{code_part}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Ошибка получения событий: {e}"

    def cmd_help(self, code: str) -> str:
        """Справка по коду"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                row = c.execute("""
                    SELECT short_title, description, recommendations
                    FROM knowledge_base
                    WHERE code = ?
                """, (code,)).fetchone()

            if not row:
                return f"❌ Нет информации для кода {code}"

            title = row[0] or ""
            desc = row[1] or ""
            rec = row[2] or ""

            result = f"📘 {code}"
            if title:
                result += f"\n{title}"
            if desc:
                result += f"\n\n{desc}"
            if rec:
                result += f"\n\n💡 Рекомендации:\n{rec}"
            return result
        except Exception as e:
            return f"❌ Ошибка: {e}"

    def cmd_explain(self, code: Optional[str] = None) -> str:
        """Объяснение последнего или указанного события"""
        if code is None:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    c = conn.cursor()
                    row = c.execute("""
                        SELECT COALESCE(code, '')
                        FROM events
                        ORDER BY id DESC
                        LIMIT 1
                    """).fetchone()
                if row and row[0]:
                    code = row[0].strip()
            except Exception:
                code = None

        if not code:
            return (
                "❌ Нет событий с code для объяснения.\n"
                "Пример:\n/explain LOW_OIL_PRESSURE"
            )
        return self.cmd_help(code)

    def cmd_sources(self) -> str:
        """Список источников знаний"""
        if not self.llm_agent:
            return "❌ LLM агент недоступен."
        sources = self.llm_agent.get_available_sources()
        if not sources:
            return "📚 Документы не загружены."
        return "📚 Документы:\n" + "\n".join(f"• {s}" for s in sources)

    def cmd_refresh(self) -> str:
        """Обновление индекса документов"""
        if not self.llm_agent:
            return "❌ LLM агент недоступен."
        result = self.llm_agent.refresh_documents()
        if result.get("status") == "success":
            return f"✅ Переиндексация завершена.\nФрагментов: {result.get('documents_count', 0)}"
        return f"❌ {result.get('message', 'Ошибка')}"

    def cmd_ask(self, question: str) -> str:
        """Вопрос к LLM"""
        if not self.llm_agent:
            return "❌ LLM агент недоступен."
        result = self.llm_agent.ask(question)
        return result.get("answer", "❌ Нет ответа")

    def process_command(self, text: str, user_id: str, chat_id: Optional[str] = None) -> Optional[str]:
        """Обработка команды"""
        if not text or not text.strip():
            return None

        text = text.strip()
        parts = text.split()
        cmd = parts[0].lower()

        print(f"🔍 Обработка команды: '{cmd}'")

        if cmd == "/status":
            return self.cmd_status()
        elif cmd == "/compact":
            return self.cmd_status(compact=True)
        elif cmd == "/last":
            n = 10
            if len(parts) > 1:
                try:
                    n = max(1, min(50, int(parts[1])))
                except ValueError:
                    pass
            return self.cmd_last(n)
        elif cmd == "/help":
            if len(parts) < 2:
                return "ℹ️ Использование:\n/help CODE\n\nПример: /help LOW_OIL_PRESSURE"
            return self.cmd_help(parts[1].upper())
        elif cmd == "/explain":
            code = parts[1].upper() if len(parts) > 1 else None
            return self.cmd_explain(code)
        elif cmd == "/sources":
            return self.cmd_sources()
        elif cmd == "/refresh":
            return self.cmd_refresh()
        elif cmd == "/ask":
            if len(parts) < 2:
                return "ℹ️ Использование:\n/ask ваш вопрос"
            question = text[len("/ask"):].strip()
            return self.cmd_ask(question)
        elif cmd == "/start":
            return (
                "👋 Привет! Я бот мониторинга ДГУ.\n\n"
                "📋 Доступные команды:\n"
                "/status - полный статус установки\n"
                "/compact - краткий статус\n"
                "/last [N] - последние N событий\n"
                "/help CODE - справка по коду\n"
                "/explain [CODE] - объяснение события\n"
                "/sources - список документов\n"
                "/refresh - обновить документы\n"
                "/ask ВОПРОС - спросить LLM"
            )
        return None