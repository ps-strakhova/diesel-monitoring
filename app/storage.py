# app/storage.py
import time
import sqlite3
from typing import Dict, List, Optional, Tuple

from .cfg_parser import BinaryBit, ValueRegister


def infer_priority(group_name: str, name: str) -> int:
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
    # Для Integer/Unsigned обычно можно писать 0 (пишем только если реально поменялось)
    # Для напряжений/частот/давления иногда ставят 0.1..1.0, но это уже тонкая настройка.
    # Начнём с 0 (строгое изменение), потом ты добавишь конкретные eps для шумных каналов.
    return 0.0


def upsert_controller(conn: sqlite3.Connection, name: str, ip: str, port: int, unit_id: int) -> int:
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
    cur = conn.cursor()

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

    for b in binaries:
        pr = infer_priority(b.group, b.bit_name)
        cur.execute(
            """
            INSERT OR IGNORE INTO register_catalog(kind, addr, bit_no, name, group_name, dtype, dimension, dec, vmin, vmax, priority, epsilon)
            VALUES ('binary_bit', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 0.0)
            """,
            (b.addr, b.bit_no_1based, f"{b.value_name}: {b.bit_name}", b.group, pr),
        )
    # --- GUARANTEE: Название установки (03000-03007(8)) ---
    # Если парсер/файл cfg не дал эту строку — всё равно добавим в каталог.
    cur.execute(
        "SELECT 1 FROM register_catalog WHERE kind='value' AND addr=? LIMIT 1",
        (3000,),
    )
    if cur.fetchone() is None:
        pr = infer_priority("Основные настройки / Имя", "Название установки")
        eps = 0.0
        cur.execute(
            """
            INSERT OR IGNORE INTO register_catalog(
              kind, addr, bit_no, name, group_name, dtype, dimension, dec, vmin, vmax, priority, epsilon
            )
            VALUES ('value', ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
            3000, "Название установки", "Основные настройки / Имя", "String", "03000-03007(8)", 0, None, None, pr, eps),
        )

    conn.commit()


def get_catalog_maps(conn):
    """
    Возвращает:
      value_map: real_addr -> catalog_id  (для value регистров)
      bit_map: (addr, bit_no) -> catalog_id  (для бинарных)
      bit_map_by_addr: addr -> catalog_id    (fallback, если в чтении доступен только addr)
    """
    value_map = {}
    bit_map = {}
    bit_map_by_addr = {}

    # value
    for cid, addr in conn.execute(
            "select id, addr from register_catalog where kind='value'"
    ).fetchall():
        value_map[int(addr)] = int(cid)

    # binary: учитываем и 'bit', и 'binary_bit'
    rows = conn.execute(
        "select id, addr, bit_no, name from register_catalog where kind in ('bit','binary_bit')"
    ).fetchall()

    for cid, addr, bit_no, name in rows:
        a = int(addr)
        b = int(bit_no) if bit_no is not None else -1
        bit_map[(a, b)] = int(cid)

        # fallback по одному addr — пригодится, если чтение возвращает только “addr-номер”
        # (у тебя addr уникальны для mains/protection, так что ок)
        if a not in bit_map_by_addr:
            bit_map_by_addr[a] = int(cid)

    return value_map, bit_map, bit_map_by_addr


def should_write_value(prev_text: str, new_text: str, epsilon: float) -> bool:
    # Если значения числовые — сравним как float, иначе текст.
    try:
        pv = float(prev_text)
        nv = float(new_text)
        return abs(nv - pv) > float(epsilon)
    except:
        return prev_text != new_text


def write_sample_if_changed(
        conn: sqlite3.Connection,
        controller_id: int,
        catalog_id: int,
        new_value_text: str,
        reason_if_write: str = "delta",
        epsilon: float = 0.0,
) -> tuple[bool, str | None]:
    """
    Returns:
      (changed, prev_value_text)
      - changed=True, если записали новое значение в samples/last_value
      - prev_value_text=None, если значения раньше не было
    """
    ts = int(time.time())
    max_attempts = 5
    attempt = 0

    while attempt < max_attempts:
        try:
            cur = conn.cursor()

            # Получаем последнее значение
            cur.execute(
                "SELECT last_ts, last_value_text FROM last_value WHERE catalog_id=?",
                (catalog_id,),
            )
            row = cur.fetchone()

            # Если новое значение None или пустое или "None" - удаляем запись
            if new_value_text is None or new_value_text == "" or new_value_text == "None":
                if row:
                    cur.execute("DELETE FROM last_value WHERE catalog_id=?", (catalog_id,))
                    conn.commit()
                    return True, row[1]
                return False, None

            if row is None:
                # Вставляем новую запись
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
                # Обновляем существующую запись
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
            if "locked" in str(e) and attempt < max_attempts - 1:
                attempt += 1
                time.sleep(0.1 * attempt)  # Увеличиваем задержку с каждой попыткой
                continue
            else:
                # Если все попытки исчерпаны или другая ошибка
                print(f"Database error in write_sample_if_changed (attempt {attempt + 1}): {e}")
                return False, None
        except Exception as e:
            print(f"Error in write_sample_if_changed: {e}")
            return False, None

    return False, None


def insert_event(conn: sqlite3.Connection, controller_id: int, severity: str, title: str, details: str = "",
                 code: str = "", catalog_id: int | None = None, filter_passed: int = 0, snapshot: str = None):
    """
    Вставляет событие в таблицу events.
    Если передан snapshot, он добавляется в details.
    """
    if snapshot:
        details = (details or "") + "\n\n" + snapshot
    ts = int(time.time())
    max_attempts = 3
    attempt = 0
    while attempt < max_attempts:
        try:
            conn.execute(
                "INSERT INTO events(ts,controller_id,severity,code,title,details,catalog_id,filter_passed) VALUES (?,?,?,?,?,?,?,?)",
                (ts, controller_id, severity, code, title, details, catalog_id, filter_passed),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_attempts - 1:
                attempt += 1
                time.sleep(0.1)
                continue
            else:
                print(f"Database error in insert_event: {e}")
                return
        except Exception as e:
            print(f"Error in insert_event: {e}")
            return

def upsert_kb_article(
        conn: sqlite3.Connection,
        code: str,
        short_title: str,
        description: str = "",
        recommendations: str = "",
) -> None:
    ts = int(time.time())
    max_attempts = 3
    attempt = 0

    while attempt < max_attempts:
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
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_attempts - 1:
                attempt += 1
                time.sleep(0.1)
                continue
            else:
                print(f"Database error in upsert_kb_article: {e}")
                return
        except Exception as e:
            print(f"Error in upsert_kb_article: {e}")
            return


def get_kb_article(conn: sqlite3.Connection, code: str) -> Optional[Tuple[str, str, str]]:
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


def ack_last_event(conn: sqlite3.Connection, ack_by: str, ack_chat_id: str, note: str = "") -> tuple[bool, str]:
    """
    Acknowledge the latest event. Returns (ok, message).
    """
    max_attempts = 3
    attempt = 0

    while attempt < max_attempts:
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
            if "locked" in str(e) and attempt < max_attempts - 1:
                attempt += 1
                time.sleep(0.1)
                continue
            else:
                print(f"Database error in ack_last_event: {e}")
                return False, f"Ошибка БД: {e}"
        except Exception as e:
            print(f"Error in ack_last_event: {e}")
            return False, f"Ошибка: {e}"


def get_last_event_code(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute("select coalesce(code,'') from events order by id desc limit 1").fetchone()
        if not row:
            return None
        code = (row[0] or "").strip()
        return code or None
    except Exception as e:
        print(f"Error in get_last_event_code: {e}")
        return None