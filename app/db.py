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