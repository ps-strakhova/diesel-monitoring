# app/yandex_webhook.py (обновлённая версия)
from __future__ import annotations
import logging
from fastapi import FastAPI, Request
from .yandex_commands import YandexCommandHandler
from .status_renderer import render_status_text

logger = logging.getLogger(__name__)

webhook_app = FastAPI()
command_handler = None
monitor_instance = None

def init_webhook_handler(monitor):
    global command_handler, monitor_instance
    # передаём monitor, чтобы handler мог получить db_path, unit_name, ya и т.д.
    command_handler = YandexCommandHandler(monitor.db_path, monitor=monitor)
    monitor_instance = monitor

@webhook_app.post("/webhook")
async def yandex_webhook(request: Request):
    global command_handler, monitor_instance

    try:
        body = await request.json()
        logger.info(f"Received webhook: {body}")

        message = body.get("message", {})
        text = message.get("text", "")
        user_id = message.get("from", {}).get("id", "unknown")
        chat_id = message.get("chat", {}).get("id")
        reply_to_msg_id = message.get("reply_to_message_id")
        current_msg_id = message.get("message_id")

        if not text:
            return {"ok": True}

        # 1. Если это ответ (реплай) на наше уведомление – отправляем статус
        if reply_to_msg_id and monitor_instance and monitor_instance.ya:
            code = monitor_instance.ya.get_code_by_message_id(str(reply_to_msg_id))
            if code:
                # Важные коды – полный статус, иначе компактный
                important_codes = {"UNIT_STOP", "AL_MAINS_FAIL", "LOW_OIL_PRESSURE", "HIGH_TEMP"}
                if code in important_codes:
                    status = render_status_text(monitor_instance.db_path)
                else:
                    status = monitor_instance.get_compact_status_from_memory()
                monitor_instance.ya.send_reply(status, reply_to_msg_id, chat_id)
                return {"ok": True}

        # 2. Обработка команд (включая обычные текстовые сообщения)
        if command_handler:
            response = command_handler.handle_command(text, str(user_id))
            if response:
                # Если есть current_msg_id – отвечаем реплаем, иначе просто отправляем
                if current_msg_id:
                    monitor_instance.ya.send_reply(response, current_msg_id, chat_id)
                else:
                    monitor_instance.ya.send(response, chat_id=chat_id)
        else:
            # Если command_handler не инициализирован – просто заглушка
            if current_msg_id and monitor_instance and monitor_instance.ya:
                monitor_instance.ya.send_reply(
                    "Бот временно недоступен. Попробуйте позже.",
                    current_msg_id, chat_id
                )

        return {"ok": True}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

def start_webhook_server(port: int = 5001):
    import uvicorn
    from threading import Thread

    def run():
        uvicorn.run(webhook_app, host="0.0.0.0", port=port, log_level="warning")

    thread = Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"Webhook server started on port {port}")