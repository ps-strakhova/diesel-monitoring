# app/db.py
import sqlite3
from typing import Iterable, Optional, Tuple
import hashlib
import secrets
import os

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS controllers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  modbus_name TEXT DEFAULT '',
  ip TEXT NOT NULL,
  port INTEGER NOT NULL,
  unit_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS register_catalog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,              -- 'value' | 'binary_bit'
  addr INTEGER NOT NULL,           -- value register address OR binary addr
  bit_no INTEGER NOT NULL DEFAULT -1,  -- -1 for 'value', 1.. for 'binary_bit'
  name TEXT NOT NULL,
  group_name TEXT NOT NULL,
  dtype TEXT,
  dimension TEXT,
  dec INTEGER,
  vmin REAL,
  vmax REAL,
  priority INTEGER NOT NULL DEFAULT 1,
  epsilon REAL NOT NULL DEFAULT 0.0,
  UNIQUE(kind, addr, bit_no)
);

CREATE TABLE IF NOT EXISTS last_value (
  catalog_id INTEGER PRIMARY KEY,
  last_ts INTEGER NOT NULL,
  last_value_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  controller_id INTEGER NOT NULL,
  catalog_id INTEGER NOT NULL,
  value_text TEXT NOT NULL,
  reason TEXT NOT NULL,
  FOREIGN KEY(controller_id) REFERENCES controllers(id),
  FOREIGN KEY(catalog_id) REFERENCES register_catalog(id)
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_cat_ts ON samples(catalog_id, ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  controller_id INTEGER NOT NULL,
  severity TEXT NOT NULL,
  code TEXT,
  title TEXT NOT NULL,
  details TEXT,
  catalog_id INTEGER,
  filter_passed INTEGER DEFAULT 0,
  FOREIGN KEY(controller_id) REFERENCES controllers(id),
  FOREIGN KEY(catalog_id) REFERENCES register_catalog(id)
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base (
  code TEXT PRIMARY KEY,
  short_title TEXT,
  description TEXT,
  recommendations TEXT,
  updated_ts INTEGER
);

CREATE TABLE IF NOT EXISTS acknowledgements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  code TEXT,
  ack_by TEXT,
  ack_chat_id TEXT,
  ack_ts INTEGER NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ack_event_id ON acknowledgements(event_id);
CREATE INDEX IF NOT EXISTS idx_ack_code ON acknowledgements(code);
"""


# ===== ФУНКЦИИ ДЛЯ МИГРАЦИЙ =====

def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Проверяет, существует ли таблица в БД"""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """Проверяет, существует ли колонка в таблице"""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def migrate_database(conn: sqlite3.Connection):
    """Выполняет миграции БД без потери данных"""

    # 1. Создаем таблицу users, если её нет
    if not table_exists(conn, "users"):
        print("📌 Создание таблицы users...")
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_ts INTEGER DEFAULT (strftime('%s', 'now')),
                last_login_ts INTEGER
            )
        """)
        print("✅ Таблица users создана")

    # 2. Создаем таблицу user_controllers, если её нет
    if not table_exists(conn, "user_controllers"):
        print("📌 Создание таблицы user_controllers...")
        conn.execute("""
            CREATE TABLE user_controllers (
                user_id INTEGER NOT NULL,
                controller_id INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(controller_id) REFERENCES controllers(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id, controller_id)
            )
        """)
        print("✅ Таблица user_controllers создана")

    # 3. Проверяем, есть ли колонка modbus_name в controllers (если нет - добавляем)
    if table_exists(conn, "controllers") and not column_exists(conn, "controllers", "modbus_name"):
        print("📌 Добавление колонки modbus_name в controllers...")
        conn.execute("ALTER TABLE controllers ADD COLUMN modbus_name TEXT DEFAULT ''")
        print("✅ Колонка modbus_name добавлена")

    conn.commit()


# ===== ФУНКЦИИ ДЛЯ ПАРОЛЕЙ =====

def hash_password(password: str, salt: Optional[str] = None) -> tuple:
    """Хеширует пароль с солью"""
    if salt is None:
        salt = secrets.token_hex(16)
    hash_obj = hashlib.sha256()
    hash_obj.update((password + salt).encode('utf-8'))
    return hash_obj.hexdigest(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Проверяет пароль"""
    hash_obj = hashlib.sha256()
    hash_obj.update((password + salt).encode('utf-8'))
    return hash_obj.hexdigest() == password_hash


def create_default_admin(conn: sqlite3.Connection):
    """Создает администратора по умолчанию, если его нет"""
    try:
        cursor = conn.execute("SELECT id FROM users WHERE username = 'admin'")
        if cursor.fetchone():
            return

        password = "admin123"
        hash_val, salt = hash_password(password)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin, created_ts) VALUES (?, ?, ?, 1, strftime('%s', 'now'))",
            ("admin", hash_val, salt)
        )
        conn.commit()
        print(f"✅ Создан администратор: admin / {password}")
        print("⚠️ СМЕНИТЕ ПАРОЛЬ ПРИ ПЕРВОМ ВХОДЕ!")
    except sqlite3.Error as e:
        print(f"❌ Ошибка создания администратора: {e}")


def connect(db_path: str) -> sqlite3.Connection:
    """Создает подключение к БД и применяет схему"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")

    # Применяем основную схему
    conn.executescript(SCHEMA)

    # Выполняем миграции (добавляем новые таблицы без потери данных)
    migrate_database(conn)

    # Создаем администратора по умолчанию
    create_default_admin(conn)

    return conn


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = "diesel.db"

    print(f"Создание/обновление базы данных {db_path}...")
    conn = connect(db_path)
    conn.close()
    print("Готово!")