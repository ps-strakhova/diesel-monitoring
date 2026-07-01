# app/storage.py
"""
Модуль для работы с хранилищем данных (БД SQLite)
"""

import time
import sqlite3
from typing import Dict, List, Optional, Tuple

from .cfg_parser import BinaryBit, ValueRegister


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def infer_priority(group_name: str, name: str) -> int:
    """Определяет приоритет сигнала на основе группы и имени"""
    g = group_name.lower()
    n = name.lower()

    # критично: защиты/аварии
    if "фиксированные защиты" in g:
        return 4

    # высоко: двигатель/генератор/нагрузка/сеть
    if any(x in g for x in ["двигатель", "генератор", "нагрузка", "сеть"]):
        return 3

    # средне: статистика (ТО, моточасы)
    if "статистика" in g:
        return 2

    # низко: информация/ethernet/4g/gps и т.п.
    if any(x in g for x in ["информация", "ethernet", "4g", "gps"]):
        return 1

    return 1


def infer_epsilon(v: ValueRegister) -> float:
    """Определяет epsilon для регистра (по умолчанию 0)"""
    return 0.0


def should_write_value(prev_text: str, new_text: str, epsilon: float) -> bool:
    """Проверяет, нужно ли записывать новое значение"""
    if new_text is None or new_text == "" or new_text == "None":
        return True

    try:
        pv = float(prev_text)
        nv = float(new_text)
        return abs(nv - pv) > float(epsilon)
    except:
        return prev_text != new_text


def _execute_with_retry(conn: sqlite3.Connection, query: str, params: tuple,
                        max_attempts: int = 3) -> bool:
    """Выполняет запрос с повторными попытками при блокировке БД"""
    attempt = 0
    while attempt < max_attempts:
        try:
            conn.execute(query, params)
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_attempts - 1:
                attempt += 1
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    return False


# ==================== ОСНОВНЫЕ ФУНКЦИИ ====================

def upsert_controller(conn: sqlite3.Connection, name: str, ip: str, port: int, unit_id: int) -> int:
    """Создает или обновляет контроллер в БД"""
    cur = conn.cursor()
    cur.execute("SELECT id FROM controllers WHERE ip=? AND port=? AND unit_id=?", (ip, port, unit_id))
    row = cur.fetchone()
    if row:
        return int(row[0])

    cur.execute(
        "INSERT INTO controllers(name, ip, port, unit_id) VALUES (?,?,?,?)",
        (name, ip, port, unit_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def load_catalog(conn: sqlite3.Connection, binaries: List[BinaryBit], values: List[ValueRegister]) -> None:
    """Загружает каталог регистров в БД"""
    cur = conn.cursor()

    # Загрузка value-регистров
    for v in values:
        pr = infer_priority(v.group, v.name)
        eps = infer_epsilon(v)
        cur.execute(
            """
            INSERT OR IGNORE INTO register_catalog(
              kind, addr, bit_no, name, group_name, dtype, dimension, dec, vmin, vmax, priority, epsilon
            )
            VALUES ('value', ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (v.addr, v.name, v.group, v.dtype, v.dimension, v.dec, v.vmin, v.vmax, pr, eps),
        )

    # Загрузка бинарных битов
    for b in binaries:
        pr = infer_priority(b.group, b.bit_name)
        cur.execute(
            """
            INSERT OR IGNORE INTO register_catalog(kind, addr, bit_no, name, group_name, dtype, dimension, dec, vmin, vmax, priority, epsilon)
            VALUES ('binary_bit', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 0.0)
            """,
            (b.addr, b.bit_no_1based, f"{b.value_name}: {b.bit_name}", b.group, pr),
        )

    # Гарантированное добавление названия установки
    cur.execute(
        "SELECT 1 FROM register_catalog WHERE kind='value' AND addr=? LIMIT 1",
        (3000,),
    )
    if cur.fetchone() is None:
        pr = infer_priority("Основные настройки / Имя", "Название установки")
        cur.execute(
            """
            INSERT OR IGNORE INTO register_catalog(
              kind, addr, bit_no, name, group_name, dtype, dimension, dec, vmin, vmax, priority, epsilon
            )
            VALUES ('value', ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
            3000, "Название установки", "Основные настройки / Имя", "String", "03000-03007(8)", 0, None, None, pr, 0.0),
        )

    conn.commit()


def get_catalog_maps(conn: sqlite3.Connection) -> Tuple[Dict[int, int], Dict[Tuple[int, int], int], Dict[int, int]]:
    """
    Возвращает:
      value_map: real_addr -> catalog_id (для value регистров)
      bit_map: (addr, bit_no) -> catalog_id (для бинарных)
      bit_map_by_addr: addr -> catalog_id (fallback)
    """
    value_map = {}
    bit_map = {}
    bit_map_by_addr = {}

    # Value регистры
    for cid, addr in conn.execute(
            "select id, addr from register_catalog where kind='value'"
    ).fetchall():
        value_map[int(addr)] = int(cid)

    # Бинарные биты
    rows = conn.execute(
        "select id, addr, bit_no, name from register_catalog where kind in ('bit','binary_bit')"
    ).fetchall()

    for cid, addr, bit_no, name in rows:
        a = int(addr)
        b = int(bit_no) if bit_no is not None else -1
        bit_map[(a, b)] = int(cid)
        if a not in bit_map_by_addr:
            bit_map_by_addr[a] = int(cid)

    return value_map, bit_map, bit_map_by_addr


def write_sample_if_changed(
        conn: sqlite3.Connection,
        controller_id: int,
        catalog_id: int,
        new_value_text: str,
        reason_if_write: str = "delta",
        epsilon: float = 0.0,
) -> Tuple[bool, Optional[str]]:
    """
    Записывает значение, если оно изменилось.
    Returns: (changed, prev_value_text)
    """
    ts = int(time.time())

    # Если значение пустое или None - удаляем запись
    if new_value_text is None or new_value_text == "" or new_value_text == "None":
        try:
            cur = conn.cursor()
            cur.execute("SELECT last_value_text FROM last_value WHERE catalog_id=?", (catalog_id,))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM last_value WHERE catalog_id=?", (catalog_id,))
                conn.commit()
                return True, row[0]
            return False, None
        except Exception as e:
            print(f"Error deleting value: {e}")
            return False, None

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_ts, last_value_text FROM last_value WHERE catalog_id=?",
            (catalog_id,),
        )
        row = cur.fetchone()

        if row is None:
            # Новая запись
            cur.execute(
                "INSERT INTO last_value(catalog_id,last_ts,last_value_text) VALUES (?,?,?)",
                (catalog_id, ts, new_value_text),
            )
            cur.execute(
                "INSERT INTO samples(ts,controller_id,catalog_id,value_text,reason) VALUES (?,?,?,?,?)",
                (ts, controller_id, catalog_id, new_value_text, reason_if_write),
            )
            conn.commit()
            return True, None

        prev_text = row[1]
        if should_write_value(prev_text, new_value_text, epsilon):
            cur.execute(
                "UPDATE last_value SET last_ts=?, last_value_text=? WHERE catalog_id=?",
                (ts, new_value_text, catalog_id),
            )
            cur.execute(
                "INSERT INTO samples(ts,controller_id,catalog_id,value_text,reason) VALUES (?,?,?,?,?)",
                (ts, controller_id, catalog_id, new_value_text, reason_if_write),
            )
            conn.commit()
            return True, prev_text

        return False, prev_text

    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            # Повторная попытка
            return write_sample_if_changed(conn, controller_id, catalog_id, new_value_text, reason_if_write, epsilon)
        print(f"Database error: {e}")
        return False, None
    except Exception as e:
        print(f"Error in write_sample_if_changed: {e}")
        return False, None


def insert_event(conn: sqlite3.Connection, controller_id: int, severity: str, title: str,
                 details: str = "", code: str = "", catalog_id: int | None = None,
                 filter_passed: int = 0, snapshot: str = None) -> None:
    """Вставляет событие в таблицу events"""
    if snapshot:
        details = (details or "") + "\n\n" + snapshot

    ts = int(time.time())
    try:
        conn.execute(
            "INSERT INTO events(ts,controller_id,severity,code,title,details,catalog_id,filter_passed) VALUES (?,?,?,?,?,?,?,?)",
            (ts, controller_id, severity, code, title, details, catalog_id, filter_passed),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            insert_event(conn, controller_id, severity, title, details, code, catalog_id, filter_passed, snapshot)
        else:
            print(f"Database error in insert_event: {e}")
    except Exception as e:
        print(f"Error in insert_event: {e}")


def upsert_kb_article(
        conn: sqlite3.Connection,
        code: str,
        short_title: str,
        description: str = "",
        recommendations: str = "",
) -> None:
    """Создает или обновляет статью в базе знаний"""
    ts = int(time.time())
    try:
        conn.execute(
            """
            INSERT INTO knowledge_base(code, short_title, description, recommendations, updated_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
              short_title=excluded.short_title,
              description=excluded.description,
              recommendations=excluded.recommendations,
              updated_ts=excluded.updated_ts
            """,
            (code, short_title, description, recommendations, ts),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            upsert_kb_article(conn, code, short_title, description, recommendations)
        else:
            print(f"Database error in upsert_kb_article: {e}")
    except Exception as e:
        print(f"Error in upsert_kb_article: {e}")


def get_kb_article(conn: sqlite3.Connection, code: str) -> Optional[Tuple[str, str, str]]:
    """Получает статью из базы знаний по коду"""
    try:
        row = conn.execute(
            "SELECT short_title, description, recommendations FROM knowledge_base WHERE code=?",
            (code,),
        ).fetchone()
        if not row:
            return None
        return str(row[0] or ""), str(row[1] or ""), str(row[2] or "")
    except Exception as e:
        print(f"Error in get_kb_article: {e}")
        return None


def ack_last_event(conn: sqlite3.Connection, ack_by: str, ack_chat_id: str, note: str = "") -> Tuple[bool, str]:
    """Подтверждает последнее событие"""
    try:
        c = conn.cursor()
        row = c.execute(
            "select id, coalesce(code,''), severity, title from events order by id desc limit 1"
        ).fetchone()
        if not row:
            return False, "Событий пока нет."

        event_id, code, severity, title = row
        ts = int(time.time())

        c.execute(
            """
            insert into acknowledgements(event_id, code, ack_by, ack_chat_id, ack_ts, note)
            values (?, ?, ?, ?, ?, ?)
            """,
            (event_id, code, ack_by, ack_chat_id, ts, note),
        )
        conn.commit()
        return True, f"✅ Принято: [{severity}] {title} ({code})"

    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            return ack_last_event(conn, ack_by, ack_chat_id, note)
        return False, f"Ошибка БД: {e}"
    except Exception as e:
        return False, f"Ошибка: {e}"


def get_last_event_code(conn: sqlite3.Connection) -> Optional[str]:
    """Получает код последнего события"""
    try:
        row = conn.execute("select coalesce(code,'') from events order by id desc limit 1").fetchone()
        if not row:
            return None
        code = (row[0] or "").strip()
        return code or None
    except Exception as e:
        print(f"Error in get_last_event_code: {e}")
        return None