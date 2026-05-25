# app/db.py
import sqlite3
from typing import Iterable, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS controllers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  modbus_name TEXT DEFAULT '',  -- НОВАЯ КОЛОНКА
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



def connect(db_path: str) -> sqlite3.Connection:
    """Создает подключение к БД и применяет схему"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    return conn


# Если вы хотите запускать этот файл отдельно для создания БД
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = "diesel.db"

    print(f"Создание базы данных {db_path}...")
    conn = connect(db_path)
    conn.close()
    print("Готово!")


    # # app/db.py
    # import sqlite3
    # from typing import Optional, Tuple, List
    #
    # SCHEMA = """
    # PRAGMA journal_mode=WAL;
    # PRAGMA synchronous=NORMAL;
    #
    # -- ============================================
    # -- ПОЛЬЗОВАТЕЛИ (минимально)
    # -- ============================================
    #
    # CREATE TABLE IF NOT EXISTS users (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   username TEXT UNIQUE NOT NULL,   -- логин
    #   password_hash TEXT NOT NULL,     -- хеш пароля
    #   role TEXT NOT NULL DEFAULT 'user'  -- 'admin' или 'user'
    # );
    #
    # -- ============================================
    # -- ДОСТУП ПОЛЬЗОВАТЕЛЕЙ К КОНТРОЛЛЕРАМ
    # -- ============================================
    #
    # CREATE TABLE IF NOT EXISTS user_controllers (
    #   user_id INTEGER NOT NULL,
    #   controller_id INTEGER NOT NULL,
    #   PRIMARY KEY (user_id, controller_id),
    #   FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    #   FOREIGN KEY (controller_id) REFERENCES controllers(id) ON DELETE CASCADE
    # );
    #
    # -- ============================================
    # -- ОСНОВНЫЕ ТАБЛИЦЫ (ваши существующие, без изменений)
    # -- ============================================
    #
    # CREATE TABLE IF NOT EXISTS controllers (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   name TEXT NOT NULL,
    #   modbus_name TEXT DEFAULT '',
    #   ip TEXT NOT NULL,
    #   port INTEGER NOT NULL,
    #   unit_id INTEGER NOT NULL
    # );
    #
    # CREATE TABLE IF NOT EXISTS register_catalog (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   kind TEXT NOT NULL,
    #   addr INTEGER NOT NULL,
    #   bit_no INTEGER NOT NULL DEFAULT -1,
    #   name TEXT NOT NULL,
    #   group_name TEXT NOT NULL,
    #   dtype TEXT,
    #   dimension TEXT,
    #   dec INTEGER,
    #   vmin REAL,
    #   vmax REAL,
    #   priority INTEGER NOT NULL DEFAULT 1,
    #   epsilon REAL NOT NULL DEFAULT 0.0,
    #   UNIQUE(kind, addr, bit_no)
    # );
    #
    # CREATE TABLE IF NOT EXISTS last_value (
    #   catalog_id INTEGER PRIMARY KEY,
    #   last_ts INTEGER NOT NULL,
    #   last_value_text TEXT NOT NULL,
    #   FOREIGN KEY(catalog_id) REFERENCES register_catalog(id)
    # );
    #
    # CREATE TABLE IF NOT EXISTS samples (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   ts INTEGER NOT NULL,
    #   controller_id INTEGER NOT NULL,
    #   catalog_id INTEGER NOT NULL,
    #   value_text TEXT NOT NULL,
    #   reason TEXT NOT NULL,
    #   FOREIGN KEY(controller_id) REFERENCES controllers(id),
    #   FOREIGN KEY(catalog_id) REFERENCES register_catalog(id)
    # );
    #
    # CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
    # CREATE INDEX IF NOT EXISTS idx_samples_cat_ts ON samples(catalog_id, ts);
    #
    # CREATE TABLE IF NOT EXISTS events (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   ts INTEGER NOT NULL,
    #   controller_id INTEGER NOT NULL,
    #   severity TEXT NOT NULL,
    #   code TEXT,
    #   title TEXT NOT NULL,
    #   details TEXT,
    #   catalog_id INTEGER,
    #   filter_passed INTEGER DEFAULT 0,
    #   FOREIGN KEY(controller_id) REFERENCES controllers(id),
    #   FOREIGN KEY(catalog_id) REFERENCES register_catalog(id)
    # );
    #
    # CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
    #
    # CREATE TABLE IF NOT EXISTS recommendations (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   code TEXT UNIQUE,
    #   severity TEXT NOT NULL,
    #   title TEXT NOT NULL,
    #   text TEXT NOT NULL
    # );
    #
    # CREATE TABLE IF NOT EXISTS knowledge_base (
    #   code TEXT PRIMARY KEY,
    #   short_title TEXT,
    #   description TEXT,
    #   recommendations TEXT,
    #   updated_ts INTEGER
    # );
    #
    # CREATE TABLE IF NOT EXISTS acknowledgements (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   event_id INTEGER NOT NULL,
    #   code TEXT,
    #   ack_by TEXT,
    #   ack_chat_id TEXT,
    #   ack_ts INTEGER NOT NULL,
    #   note TEXT,
    #   FOREIGN KEY(event_id) REFERENCES events(id)
    # );
    #
    # CREATE INDEX IF NOT EXISTS idx_ack_event_id ON acknowledgements(event_id);
    # CREATE INDEX IF NOT EXISTS idx_ack_code ON acknowledgements(code);
    #
    # CREATE TABLE IF NOT EXISTS ai_dialog_history (
    #   id INTEGER PRIMARY KEY AUTOINCREMENT,
    #   ts INTEGER NOT NULL,
    #   user_id INTEGER,
    #   username TEXT,
    #   request_text TEXT NOT NULL,
    #   response_text TEXT NOT NULL,
    #   source TEXT,
    #   model_name TEXT,
    #   FOREIGN KEY(user_id) REFERENCES users(id)
    # );
    # """
    #
    #
    # def connect(db_path: str) -> sqlite3.Connection:
    #     """Создает подключение к БД и применяет схему"""
    #     conn = sqlite3.connect(db_path, check_same_thread=False)
    #     conn.execute("PRAGMA foreign_keys=ON;")
    #     conn.executescript(SCHEMA)
    #     return conn
    #
    #
    # # ============================================
    # # ФУНКЦИИ ДЛЯ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ
    # # ============================================
    #
    # def create_user(conn: sqlite3.Connection, username: str, password_hash: str, role: str = 'user') -> int:
    #     """Создает нового пользователя"""
    #     cursor = conn.execute(
    #         "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
    #         (username, password_hash, role)
    #     )
    #     return cursor.lastrowid
    #
    #
    # def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[Tuple]:
    #     """Получает пользователя по логину"""
    #     cursor = conn.execute("SELECT * FROM users WHERE username = ?", (username,))
    #     return cursor.fetchone()
    #
    #
    # def grant_controller_access(conn: sqlite3.Connection, user_id: int, controller_id: int) -> bool:
    #     """Выдает пользователю доступ к контроллеру"""
    #     try:
    #         conn.execute(
    #             "INSERT INTO user_controllers (user_id, controller_id) VALUES (?, ?)",
    #             (user_id, controller_id)
    #         )
    #         return True
    #     except sqlite3.IntegrityError:
    #         return False  # уже есть доступ
    #
    #
    # def revoke_controller_access(conn: sqlite3.Connection, user_id: int, controller_id: int) -> bool:
    #     """Отзывает доступ пользователя к контроллеру"""
    #     conn.execute(
    #         "DELETE FROM user_controllers WHERE user_id = ? AND controller_id = ?",
    #         (user_id, controller_id)
    #     )
    #     return conn.total_changes > 0
    #
    #
    # def get_user_controllers(conn: sqlite3.Connection, user_id: int) -> List[Tuple]:
    #     """Возвращает список ID контроллеров, доступных пользователю"""
    #     cursor = conn.execute(
    #         "SELECT controller_id FROM user_controllers WHERE user_id = ?",
    #         (user_id,)
    #     )
    #     return [row[0] for row in cursor.fetchall()]
    #
    #
    # def get_visible_controllers(conn: sqlite3.Connection, user_id: int) -> List[Tuple]:
    #     """Возвращает список контроллеров, которые видит пользователь"""
    #     # Получаем роль пользователя
    #     cursor = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    #     row = cursor.fetchone()
    #     if not row:
    #         return []
    #
    #     role = row[0]
    #
    #     if role == 'admin':
    #         # Админ видит ВСЕ контроллеры
    #         cursor = conn.execute("SELECT * FROM controllers ORDER BY id")
    #         return cursor.fetchall()
    #     else:
    #         # Обычный пользователь видит только выданные контроллеры
    #         cursor = conn.execute("""
    #             SELECT c.*
    #             FROM controllers c
    #             INNER JOIN user_controllers uc ON c.id = uc.controller_id
    #             WHERE uc.user_id = ?
    #             ORDER BY c.id
    #         """, (user_id,))
    #         return cursor.fetchall()
    #
    #
    # def user_has_access(conn: sqlite3.Connection, user_id: int, controller_id: int) -> bool:
    #     """Проверяет, есть ли у пользователя доступ к контроллеру"""
    #     cursor = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    #     row = cursor.fetchone()
    #     if not row:
    #         return False
    #     if row[0] == 'admin':
    #         return True
    #
    #     cursor = conn.execute(
    #         "SELECT 1 FROM user_controllers WHERE user_id = ? AND controller_id = ?",
    #         (user_id, controller_id)
    #     )
    #     return cursor.fetchone() is not None
    #
    #
    # # ============================================
    # # ЗАПУСК ДЛЯ СОЗДАНИЯ БД
    # # ============================================
    #
    # if __name__ == "__main__":
    #     import sys
    #     import hashlib
    #
    #     db_path = sys.argv[1] if len(sys.argv) > 1 else "diesel.db"
    #
    #     print(f"Создание базы данных {db_path}...")
    #     conn = connect(db_path)
    #
    #     # Создаем тестового админа (логин: admin, пароль: admin123)
    #     admin_hash = hashlib.sha256("admin123".encode()).hexdigest()
    #     try:
    #         create_user(conn, "admin", admin_hash, "admin")
    #         print("✅ Админ создан: admin / admin123")
    #     except sqlite3.IntegrityError:
    #         print("⚠️ Админ уже существует")
    #
    #     # Создаем тестового пользователя (логин: user, пароль: user123)
    #     user_hash = hashlib.sha256("user123".encode()).hexdigest()
    #     try:
    #         create_user(conn, "user", user_hash, "user")
    #         print("✅ Пользователь создан: user / user123")
    #     except sqlite3.IntegrityError:
    #         print("⚠️ Пользователь уже существует")
    #
    #     conn.close()
    #     print("Готово!")
