#config
from dataclasses import dataclass
import os

@dataclass
class ModbusConfig:
    ip: str
    port: int = 502
    unit_id: int = 1

# === твой контроллер ===
CONTROLLER = ModbusConfig(
    ip=os.getenv("CONTROLLER_IP", "xxxxxxxx"),
    port=int(os.getenv("CONTROLLER_PORT", "502")),
    unit_id=int(os.getenv("UNIT_ID", "1")),
)

# Адреса бинарных (00000..00304 => 305 бит)
ERROR_BITS_START = 0
ERROR_BITS_COUNT = 305

# Если у тебя читалось как coils — оставь True.
# Если окажется, что это discrete inputs (fn=02), переключишь на False.
READ_AS_COILS = True

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "2"))

DB_PATH = os.getenv("DB_PATH", "diesel.db")

# Где лежит cfgmodbus.txt
CFG_PATH = os.getenv("CFG_PATH", "data/cfgmodbus.txt")

# --- Ключ для “смещения адресов” ---
# В Modbus документации иногда адреса “человеческие”, а библиотека ждёт 0-based.
# Если видишь, что регистр 01000 читается только по адресу 999 — поставь -1.
REGISTER_ADDRESS_OFFSET = int(os.getenv("REGISTER_ADDRESS_OFFSET", "0"))

# --- Логирование ---
# 1) Снимок всех “важных” аналоговых регистров раз в N секунд (для трендов)
SNAPSHOT_EVERY_SEC = int(os.getenv("SNAPSHOT_EVERY_SEC", "300"))  # 5 минут

# 2) Delta-логирование: писать значение если изменилось больше чем epsilon
DEFAULT_EPSILON = float(os.getenv("DEFAULT_EPSILON", "0.0"))

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

TELEGRAM_NOTIFY_CHAT_ID = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
TELEGRAM_COMMAND_CHAT_ID = os.getenv("TELEGRAM_COMMAND_CHAT_ID", "").strip()

# Яндекс Мессенджер
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_CHAT_ID = os.getenv("YANDEX_CHAT_ID", "").strip()