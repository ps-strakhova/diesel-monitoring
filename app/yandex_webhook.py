from fastapi import FastAPI, Request, HTTPException
import logging
from .yandex_commands import YandexCommandHandler

logger = logging.getLogger(__name__)

webhook_app = FastAPI()
command_handler = None
monitor_instance = None


def init_webhook_handler(monitor):
    """Инициализация обработчика вебхука"""
    global command_handler, monitor_instance
    command_handler = YandexCommandHandler(monitor.conn)
    monitor_instance = monitor


@webhook_app.post("/webhook")
async def yandex_webhook(request: Request):
    """Обработка входящих сообщений из Яндекс.Мессенджера"""
    global command_handler, monitor_instance

    try:
        body = await request.json()
        logger.info(f"Received webhook: {body}")

        # Извлекаем сообщение (формат зависит от API Яндекс.Мессенджера)
        message = body.get("message", {}).get("text", "")
        user_id = body.get("message", {}).get("from", {}).get("id", "unknown")
        chat_id = body.get("message", {}).get("chat", {}).get("id")

        if not message:
            return {"ok": True}

        # Обрабатываем команду
        if command_handler:
            response = await command_handler.handle_command(message, str(user_id))

            # Отправляем ответ через монитор
            if monitor_instance and monitor_instance.ya:
                monitor_instance.ya.send(response, chat_id=chat_id)

        return {"ok": True}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}


def start_webhook_server(port: int = 5001):
    """Запуск сервера вебхука"""
    import uvicorn
    from threading import Thread

    def run():
        uvicorn.run(webhook_app, host="0.0.0.0", port=port, log_level="warning")

    thread = Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"Webhook server started on port {port}")