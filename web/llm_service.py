"""
Сервис для LLM агента
"""

import sys
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self, db_path):
        self.db_path = db_path
        self.llm_agent = None

        # Добавляем путь к llm_agent
        LLM_AGENT_PATH = Path(__file__).parent.parent / "llm_agent"
        if str(LLM_AGENT_PATH) not in sys.path:
            sys.path.insert(0, str(LLM_AGENT_PATH))

    def init_agent(self):
        try:
            from simple_agent import DieselLLMAgent
            self.llm_agent = DieselLLMAgent(auto_index=True)
            return True
        except Exception as e:
            logger.error(f"Ошибка инициализации LLM агента: {e}")
            return False

    def get_status(self):
        if self.llm_agent is None:
            return {"status": "not_initialized", "message": "LLM агент не инициализирован"}
        try:
            stats = self.llm_agent.get_stats()
            return {"status": "ready", "stats": stats}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def ask(self, request):
        if self.llm_agent is None:
            return {"error": "LLM агент не готов"}

        question = request.get("question", "").strip()
        if not question:
            return {"error": "Вопрос не может быть пустым"}

        monitoring_data = request.get("monitoring_data", {})

        try:
            if monitoring_data:
                result = self.llm_agent.ask_with_status(question, monitoring_data)
            else:
                result = self.llm_agent.ask(question)

            answer_cleaned = result["answer"].replace('<br>', '\n').replace('<br/>', '\n')
            return {
                "success": True,
                "answer": answer_cleaned,
                "sources": result.get("sources", []),
                "question": question
            }
        except Exception as e:
            logger.error(f"LLM ошибка: {e}")
            return {
                "success": False,
                "answer": f"Извините, произошла ошибка: {str(e)}",
                "sources": [],
                "question": question
            }

    async def get_sources(self):
        if self.llm_agent is None:
            return {"error": "LLM агент не готов"}
        try:
            sources = self.llm_agent.get_available_sources()
            return {"sources": sources}
        except Exception as e:
            return {"error": str(e)}

    async def refresh(self):
        if self.llm_agent is None:
            return {"error": "LLM агент не готов"}
        try:
            result = self.llm_agent.refresh_documents()
            return result
        except Exception as e:
            return {"error": str(e)}

    async def generate_report(self, request):
        try:
            from llm_agent.report_generator import ReportGenerator

            report_type = request.get("report_type", "claim")
            monitoring_data = request.get("monitoring_data", {})
            conversation_history = request.get("conversation_history", [])
            controller_id = request.get("controller_id")

            report_gen = ReportGenerator(str(self.db_path) if self.db_path else None)

            if report_type == "claim":
                docx_bytes = report_gen.generate_claim_act(
                    monitoring_data, conversation_history, {"contact_person": "Сервисный инженер"}
                )
                filename = f"Акт_Заявка_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            elif report_type == "work_order":
                docx_bytes = report_gen.generate_work_order(monitoring_data)
                filename = f"Заказ_наряд_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            else:
                docx_bytes = report_gen.generate_claim_act(monitoring_data, conversation_history, {})
                filename = f"Акт_обслуживания_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"

            return {
                "content": docx_bytes,
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "filename": filename
            }
        except Exception as e:
            return {"error": str(e)}