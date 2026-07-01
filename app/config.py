# app/config.py
"""
Конфигурация приложения
"""

import os
from dataclasses import dataclass


@dataclass
class ModbusConfig:
    """Конфигурация Modbus подключения"""
    ip: str
    port: int = 502
    unit_id: int = 1


# ==================== КОНТРОЛЛЕР ====================
CONTROLLER = ModbusConfig(
    ip=os.getenv("CONTROLLER_IP", "192.168.1.100"),
    port=int(os.getenv("CONTROLLER_PORT", "502")),
    unit_id=int(os.getenv("UNIT_ID", "1")),
)

# ==================== АДРЕСА БИНАРНЫХ СИГНАЛОВ ====================
ERROR_BITS_START = 0
ERROR_BITS_COUNT = 305
READ_AS_COILS = True  # True - coils (fn=01), False - discrete inputs (fn=02)

# ==================== ИНТЕРВАЛЫ ====================
POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "2"))
SNAPSHOT_EVERY_SEC = int(os.getenv("SNAPSHOT_EVERY_SEC", "300"))  # 5 минут

# ==================== ПУТИ ====================
DB_PATH = os.getenv("DB_PATH", "diesel.db")
CFG_PATH = os.getenv("CFG_PATH", "data/cfgmodbus.txt")

# ==================== MODBUS ====================
REGISTER_ADDRESS_OFFSET = int(os.getenv("REGISTER_ADDRESS_OFFSET", "0"))
DEFAULT_EPSILON = float(os.getenv("DEFAULT_EPSILON", "0.0"))

# ==================== TELEGRAM ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_NOTIFY_CHAT_ID = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
TELEGRAM_COMMAND_CHAT_ID = os.getenv("TELEGRAM_COMMAND_CHAT_ID", "").strip()

# ==================== YANDEX MESSENGER ====================
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_CHAT_ID = os.getenv("YANDEX_CHAT_ID", "").strip()
YANDEX_WEBHOOK_PORT = int(os.getenv("YANDEX_WEBHOOK_PORT", "5002"))