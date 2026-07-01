"""
Работа с базой данных
"""

import sqlite3
from pathlib import Path
import os
import logging

logger = logging.getLogger(__name__)

# Поиск БД
DB_PATH = None
possible_paths = [
    os.getenv("DB_PATH", ""),
    str(Path(__file__).parent.parent / "diesel.db"),
    str(Path(__file__).parent / "diesel.db"),
    r"C:\Users\strak\OneDrive\Рабочий стол\diesel\diesel.db"
]

for path in possible_paths:
    if path and Path(path).exists():
        DB_PATH = Path(path).resolve()
        logger.info(f"✅ Найдена БД: {DB_PATH}")
        break

if not DB_PATH:
    logger.error("❌ База данных не найдена!")

def get_db_connection():
    """Создает новое соединение с БД"""
    if DB_PATH is None:
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        return None

def get_db_dependency():
    """Зависимость FastAPI для получения соединения с БД"""
    conn = get_db_connection()
    if conn is None:
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()

def get_controller_display_name(row: dict) -> str:
    """Формирует отображаемое имя контроллера"""
    if row.get('modbus_name') and row['modbus_name'].strip():
        return row['modbus_name']
    if row.get('name') and row['name'].strip():
        return row['name']
    if row.get('ip') and row['ip'].strip():
        return f"ДГУ ({row['ip']})"
    return f"ДГУ {row.get('controller_id', '?')}"