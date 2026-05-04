from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import os
from pathlib import Path
import logging
import re
import csv
import io
import time
import sys
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Определяем пути
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Создаем папки если их нет
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "js").mkdir(exist_ok=True)
(STATIC_DIR / "images").mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

# Создаем FastAPI приложение
app = FastAPI(title="Diesel Monitoring Web")

# Подключаем статические файлы и шаблоны
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Конфигурация
EXCLUDED_NOTIFICATION_CODES = [
    'BIT_20',  # Импульс останова
    'BIT_36', 'BIT_39',  # служебные
    'BIT_41', 'BIT_42', 'BIT_44', 'BIT_45', 'BIT_46', 'BIT_47',
    'BIT_48', 'BIT_49', 'BIT_50', 'BIT_51', 'BIT_52', 'BIT_53',
]

# Поиск БД
DB_PATH = None
possible_paths = [
    os.getenv("DB_PATH", ""),
    str(BASE_DIR.parent / "diesel.db"),
    str(BASE_DIR / "diesel.db"),
    r"C:\Users\strak\OneDrive\Рабочий стол\diesel\diesel.db"
]

for path in possible_paths:
    if path and Path(path).exists():
        DB_PATH = Path(path).resolve()
        logger.info(f"✅ Найдена БД: {DB_PATH}")
        break

if not DB_PATH:
    logger.error("❌ База данных не найдена! Пути поиска: %s", possible_paths)
    DB_PATH = None


# Функции для работы с БД
def get_db_connection():
    """Создает новое соединение с БД."""
    if DB_PATH is None:
        logger.error("Попытка подключиться к БД, но путь не задан.")
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        return None


def get_db_dependency():
    """Зависимость FastAPI для получения и автоматического закрытия соединения с БД."""
    conn = get_db_connection()
    if conn is None:
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def clean_signal_name(name: str) -> str:
    """Убирает цифры и лишние символы в начале названия сигнала"""
    if not name:
        return name
    cleaned = re.sub(r'^\d+\s+', '', str(name))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


# API Эндпоинты
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "Diesel Monitoring",
            "now": datetime.now()
        }
    )


@app.get("/api/controllers")
async def get_controllers(db: sqlite3.Connection = Depends(get_db_dependency)) -> List[Dict[str, Any]]:
    """Возвращает список всех контроллеров."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        cursor = db.execute("SELECT id, name, ip, port, unit_id FROM controllers ORDER BY name")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка получения контроллеров: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/controller-name/{controller_id}")
async def get_controller_name(
        controller_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    """Возвращает имя контроллера (сначала modbus_name, потом name, с IP)"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        cursor = db.execute(
            "SELECT name, modbus_name, ip FROM controllers WHERE id = ?",
            (controller_id,)
        )
        row = cursor.fetchone()

        if not row:
            return {"name": f"ДГУ {controller_id}", "ip": "", "id": controller_id}

        # Сначала пробуем modbus_name, если есть и не пустой, иначе name
        if row['modbus_name'] and row['modbus_name'].strip():
            display_name = row['modbus_name']
        else:
            display_name = row['name']

        return {
            "name": display_name,
            "ip": row['ip'] or "",
            "id": controller_id
        }

    except Exception as e:
        logger.error(f"Ошибка получения имени контроллера: {e}")
        return {"name": f"ДГУ {controller_id}", "ip": "", "id": controller_id}


@app.get("/api/notifications")
async def get_notifications(
        controller_id: Optional[int] = None,
        date: Optional[str] = None,
        limit: int = 150,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> List[Dict[str, Any]]:
    """Возвращает уведомления с filter_passed=1"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        if date:
            selected_date = datetime.strptime(date, "%Y-%m-%d")
        else:
            selected_date = datetime.now()

        start_of_day = int(datetime(selected_date.year, selected_date.month, selected_date.day).timestamp())
        end_of_day = start_of_day + 86399

        # Только filter_passed = 1, УБИРАЕМ should_show_in_web
        query = """
            SELECT e.id, e.ts, e.code, e.title, e.details, e.controller_id, e.catalog_id,
                   c.name as controller_name, c.modbus_name, c.ip
            FROM events e
            JOIN controllers c ON c.id = e.controller_id
            WHERE e.ts >= ? AND e.ts <= ?
                AND e.filter_passed = 1
        """
        params: List[Any] = [start_of_day, end_of_day]

        if controller_id:
            query += " AND e.controller_id = ?"
            params.append(controller_id)

        query += " ORDER BY e.ts DESC LIMIT ?"
        params.append(limit)

        cursor = db.execute(query, params)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            item = dict(row)

            dt = datetime.fromtimestamp(item['ts'])
            item['time_msk'] = dt.strftime("%H:%M:%S")
            item['date_msk'] = dt.strftime("%d.%m.%Y")
            item['clean_title'] = clean_signal_name(item['title'])

            # Формируем имя установки
            if item.get('modbus_name') and item['modbus_name'].strip():
                controller_display = item['modbus_name']
            elif item.get('name') and item['name'].strip():
                controller_display = item['name']
            elif item.get('ip') and item['ip'].strip():
                controller_display = f"ДГУ ({item['ip']})"
            else:
                controller_display = f"ДГУ {item['controller_id']}"
            item['controller_display'] = controller_display

            details = item['details'] or ''
            if 'addr=' in details:
                if 'ВКЛ' in details:
                    item['clean_details'] = 'Сигнал ВКЛ'
                elif 'ВЫКЛ' in details:
                    item['clean_details'] = 'Сигнал ВЫКЛ'
                else:
                    item['clean_details'] = details.split(' (')[0] if '(' in details else details
            else:
                item['clean_details'] = details

            title_lower = (item['title'] or '').lower()
            code = (item['code'] or '').upper()

            # ========== СПЕЦОБРАБОТКА ДЛЯ BIT_230 и BIT_257 ==========
            if code in ('BIT_230', 'BIT_257'):
                if 'ВЫКЛ' in (item.get('clean_details') or ''):
                    item['severity'] = 'success'
                    item['icon'] = '✅'
                else:  # ВКЛ
                    item['severity'] = 'warn'
                    item['icon'] = '⚠️'
                result.append(item)
                continue

            # ========== СПЕЦОБРАБОТКА ДЛЯ BIT_43 ==========
            if code == 'BIT_43' or (item['title'] and 'Генератор в норме' in item['title']):
                item['severity'] = 'info'
                item['icon'] = 'ℹ️'
                result.append(item)
                continue

            # ========== ОСНОВНАЯ ЛОГИКА ОПРЕДЕЛЕНИЯ SEVERITY ==========
            if any(k in code for k in
                   ['NORMAL', 'OK', 'RESTORED', 'BACK_TO_NORMAL', 'RECOVERED', 'FREQ_NORMAL', 'MAINS_OK']):
                item['severity'] = 'success'
                item['icon'] = '✅'
            elif 'норма' in title_lower or 'восстанов' in title_lower or 'норме' in title_lower:
                item['severity'] = 'success'
                item['icon'] = '✅'
            elif any(k in title_lower for k in ['авар', 'критич', 'alarm', 'ост', 'shutdown', 'стоп', 'рво', 'пожар']):
                item['severity'] = 'alarm'
                item['icon'] = '🚨'
            elif any(k in title_lower for k in ['прд', 'пред', 'warn', 'низк', 'высок', 'превыш', 'заниж']):
                item['severity'] = 'warn'
                item['icon'] = '⚠️'
            else:
                item['severity'] = 'info'
                item['icon'] = 'ℹ️'

            result.append(item)
        return result
    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка получения уведомлений: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/bit-info/{addr}")
async def get_bit_info(
        addr: int,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    """Возвращает информацию о бите по его адресу"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        cursor = db.execute("""
            SELECT name, group_name, priority, addr
            FROM register_catalog
            WHERE kind = 'binary_bit' AND addr = ?
        """, (addr,))
        row = cursor.fetchone()

        if row:
            return {
                "addr": row['addr'],
                "name": row['name'],
                "group_name": row['group_name'],
                "priority": row['priority']
            }
        else:
            return {"addr": addr, "name": f"BIT_{addr}", "group_name": "", "priority": 1}
    except Exception as e:
        logger.error(f"Ошибка получения информации о бите: {e}")
        return {"addr": addr, "name": f"BIT_{addr}", "group_name": "", "priority": 1}

@app.get("/api/status-snapshot/{notif_id}")
async def get_status_snapshot(
        notif_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        cursor = db.execute("SELECT ts, controller_id FROM events WHERE id = ?", (notif_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Notification not found")

        notif_ts = row['ts']

        snapshot_params = [
            (1000, "Обороты", "об/мин"),
            (1036, "АКБ", "В"),
            (1038, "Масло", "Бар"),
            (1039, "ОЖ", "°C"),
            (1040, "Топливо", "%"),
            (1029, "Частота", "Гц"),
        ]

        snapshot = {}
        for addr, name, unit in snapshot_params:
            cursor = db.execute("""
                SELECT s.value_text FROM samples s
                JOIN register_catalog rc ON rc.id = s.catalog_id
                WHERE rc.addr = ? AND s.ts <= ?
                ORDER BY s.ts DESC LIMIT 1
            """, (addr, notif_ts))
            sample = cursor.fetchone()

            if sample and sample[0] is not None:
                try:
                    value = float(sample[0])
                    # Невалидные значения
                    if value == 32768 or value < 0 or value > 1e6:
                        formatted = "—"
                    else:
                        if addr in (1039, 1040):
                            formatted = f"{value:.0f} {unit}".strip()
                        elif addr == 1038:
                            formatted = f"{value:.1f} {unit}".strip()
                        else:
                            formatted = f"{value:.1f} {unit}".strip()
                except (ValueError, TypeError):
                    formatted = str(sample[0])
            else:
                formatted = "—"

            snapshot[name] = formatted

        return snapshot

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка получения снимка статуса: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/status/{controller_id}")
async def get_full_status(
        controller_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    """Возвращает полный текущий статус установки."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        unit_name = "ДГУ"
        cursor = db.execute("""
            SELECT last_value_text FROM last_value lv
            JOIN register_catalog rc ON rc.id = lv.catalog_id
            WHERE rc.addr = 3000
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row and row[0]:
            unit_name = row[0]

        params: Dict[str, Any] = {}

        full_status_params = {
            'rpm': 1000,
            'p_load': 1001,
            'f_gen': 1017,
            'u_gen_l1': 1018,
            'u_gen_l2': 1019,
            'u_gen_l3': 1020,
            'i_l1': 1024,
            'i_l2': 1025,
            'i_l3': 1026,
            'f_mains': 1029,
            'u_mains_l1': 1030,
            'u_mains_l2': 1031,
            'u_mains_l3': 1032,
            'battery': 1036,
            'oil': 1038,
            'temp': 1039,
            'fuel': 1040,
            'engine_state': 1286,
        }

        for name, addr in full_status_params.items():
            cursor = db.execute("""
                SELECT lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.addr = ?
                LIMIT 1
            """, (addr,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                try:
                    params[name] = float(row[0])
                except ValueError:
                    params[name] = row[0]
            else:
                params[name] = None

        mode_addrs = {54: 'mode_off', 55: 'mode_man', 56: 'mode_auto', 57: 'mode_test'}
        modes = {}
        for addr, name in mode_addrs.items():
            cursor = db.execute("""
                SELECT lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.addr = ?
                LIMIT 1
            """, (addr,))
            row = cursor.fetchone()
            modes[name] = int(row[0]) if row and row[0] else 0

        if modes['mode_off'] == 1:
            mode = "ВЫКЛ"
        elif modes['mode_man'] == 1:
            mode = "РУЧН"
        elif modes['mode_auto'] == 1:
            mode = "АВТО"
        elif modes['mode_test'] == 1:
            mode = "ТЕСТ"
        else:
            mode = "неизвестно"

        cursor = db.execute("""
            SELECT lv.last_value_text
            FROM last_value lv
            JOIN register_catalog rc ON rc.id = lv.catalog_id
            WHERE rc.addr = 44
            LIMIT 1
        """)
        row = cursor.fetchone()
        mains_ok = int(row[0]) if row and row[0] else None
        mains_state = "нет данных" if mains_ok is None else ("✅ в норме" if mains_ok == 1 else "🚨 АВАРИЯ")

        cursor = db.execute("""
            SELECT rc.addr, lv.last_value_text
            FROM last_value lv
            JOIN register_catalog rc ON rc.id = lv.catalog_id
            WHERE rc.addr IN (149, 150)
        """)
        contacts = {row[0]: row[1] for row in cursor.fetchall()}

        engine_states = {
            0: "Инициализ", 1: "Готов", 2: "Не готов", 3: "Престарт",
            4: "Прокрутка", 5: "Пауза", 6: "Запуск", 7: "В работе",
            8: "Нагрузка", 9: "ПлавнРазгр", 10: "Охлаждение", 11: "Стоп",
            12: "Остановка", 13: "Продув", 14: "Экстр РУЧН", 15: "ПлавнНагр",
            16: "ОжидСтоп", 17: "ОстПродув", 18: "Возбуждение", 19: "Прогрев",
            20: "Без нагр"
        }
        engine_state_val = params.get('engine_state')
        engine_state = engine_states.get(int(engine_state_val), "—") if engine_state_val is not None else "—"

        status_data = {
            "unit_name": unit_name,
            "general": {
                "engine_state": engine_state,
                "mode": mode,
                "mains_state": mains_state,
                "mains_ok": mains_ok,
                "contacts_avs": "ВКЛ" if contacts.get(149) == '1' else "ВЫКЛ",
                "contacts_avg": "ВКЛ" if contacts.get(150) == '1' else "ВЫКЛ",
            },
            "engine": {
                "rpm": f"{params.get('rpm', 0):.0f}" if params.get('rpm') is not None else "—",
                "p_load": f"{params.get('p_load', 0):.1f}" if params.get('p_load') is not None else "—",
            },
            "generator": {
                "f_gen": f"{params.get('f_gen', 0):.1f}" if params.get('f_gen') is not None else "—",
                "u_gen_l1": f"{params.get('u_gen_l1', 0):.1f}" if params.get('u_gen_l1') is not None else "—",
                "u_gen_l2": f"{params.get('u_gen_l2', 0):.1f}" if params.get('u_gen_l2') is not None else "—",
                "u_gen_l3": f"{params.get('u_gen_l3', 0):.1f}" if params.get('u_gen_l3') is not None else "—",
            },
            "current": {
                "i_l1": f"{params.get('i_l1', 0):.1f}" if params.get('i_l1') is not None else "—",
                "i_l2": f"{params.get('i_l2', 0):.1f}" if params.get('i_l2') is not None else "—",
                "i_l3": f"{params.get('i_l3', 0):.1f}" if params.get('i_l3') is not None else "—",
            },
            "mains": {
                "f_mains": f"{params.get('f_mains', 0):.1f}" if params.get('f_mains') is not None else "—",
                "u_mains_l1": f"{params.get('u_mains_l1', 0):.1f}" if params.get('u_mains_l1') is not None else "—",
                "u_mains_l2": f"{params.get('u_mains_l2', 0):.1f}" if params.get('u_mains_l2') is not None else "—",
                "u_mains_l3": f"{params.get('u_mains_l3', 0):.1f}" if params.get('u_mains_l3') is not None else "—",
            },
            "sensors": {
                "battery": f"{params.get('battery', 0):.1f}" if params.get('battery') is not None else "—",
                "oil": f"{params.get('oil', 0):.1f}" if params.get('oil') is not None else "—",
                "temp": f"{params.get('temp', 0):.0f}" if params.get('temp') is not None else "—",
                "fuel": f"{params.get('fuel', 0):.0f}" if params.get('fuel') is not None else "—",
            }
        }

        return {
            "status_text": "",
            "status_data": status_data,
            "params": params,
            "mode": mode,
            "engine_state": engine_state,
            "mains_state": mains_state,
            "unit_name": unit_name
        }
    except Exception as e:
        logger.error(f"❌ Ошибка получения статуса для контроллера {controller_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/current-bits-status")
async def get_current_bits_status(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    """Возвращает текущее состояние важных битов для отображения в статусе"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Важные адреса битов (те же, что и в IMPORTANT_SNAPSHOT_ADDRS)
        important_addrs = [36, 37, 38, 39, 40, 43, 44, 69, 70, 71, 149, 150]

        placeholders = ','.join('?' * len(important_addrs))

        query = f"""
            SELECT rc.addr, rc.name, lv.last_value_text
            FROM last_value lv
            JOIN register_catalog rc ON rc.id = lv.catalog_id
            WHERE rc.kind = 'binary_bit' 
            AND rc.addr IN ({placeholders})
        """

        cursor = db.execute(query, important_addrs)
        rows = cursor.fetchall()

        result = {}
        for row in rows:
            addr = row['addr']
            raw_name = row['name'] or f"BIT_{addr}"
            clean_name = clean_signal_name(raw_name)
            state = "ВКЛ" if row['last_value_text'] == '1' else "ВЫКЛ"

            result[addr] = {
                "name": clean_name,
                "state": state,
                "state_num": int(row['last_value_text'] or 0)
            }

        return {
            "status": "ok",
            "bits": result,
            "timestamp": int(time.time())
        }

    except Exception as e:
        logger.error(f"Ошибка получения статуса битов: {e}")
        return {"status": "error", "bits": {}, "timestamp": 0}

@app.get("/api/current-alerts")
async def get_all_current_alerts(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> List[Dict[str, Any]]:
    """Возвращает активные аварии и предупреждения для всех установок или конкретной"""
    if db is None:
        return []

    try:
        alerts = []

        # Получаем список контроллеров
        if controller_id:
            cursor = db.execute("SELECT id, name FROM controllers WHERE id = ?", (controller_id,))
            controllers = cursor.fetchall()
        else:
            cursor = db.execute("SELECT id, name FROM controllers")
            controllers = cursor.fetchall()

        for ctrl in controllers:
            ctrl_id = ctrl["id"]
            ctrl_name = ctrl["name"]

            # Получаем активные битовые сигналы
            cursor = db.execute("""
                SELECT rc.name, rc.addr, lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.kind='binary_bit' AND lv.last_value_text='1'
                ORDER BY rc.addr
            """)

            for row in cursor.fetchall():
                name, addr, val = row

                cursor_check = db.execute("""
                                    SELECT 1 FROM events 
                                    WHERE code = ? AND filter_passed = 1 
                                    LIMIT 1
                                """, (f"BIT_{addr}",))
                if not cursor_check.fetchone():
                    continue  # Пропускаем, если в Telegram не отправлялось

                clean_name = clean_signal_name(name)
                title_lower = (name or '').lower()

                if any(k in title_lower for k in
                       ['авар', 'критич', 'alarm', 'ост', 'shutdown', 'стоп', 'рво', 'пожар']):
                    alerts.append({
                        'severity': 'alarm',
                        'code': f'BIT_{addr}',
                        'title': clean_name,
                        'details': 'Активна аварийная защита',
                        'controller_id': ctrl_id,
                        'controller_name': ctrl_name
                    })
                elif any(k in title_lower for k in ['прд', 'пред', 'warn', 'низк', 'высок']):
                    alerts.append({
                        'severity': 'warn',
                        'code': f'BIT_{addr}',
                        'title': clean_name,
                        'details': 'Активно предупреждение',
                        'controller_id': ctrl_id,
                        'controller_name': ctrl_name
                    })

            # Проверяем критические аналоговые параметры для этой установки
            cursor = db.execute("""
                SELECT rc.addr, rc.name, lv.last_value_text
                FROM last_value lv
                JOIN register_catalog rc ON rc.id = lv.catalog_id
                WHERE rc.addr IN (1036, 1038, 1039, 1040, 1029)
                AND lv.last_value_text IS NOT NULL
            """)

            for row in cursor.fetchall():
                addr, name, value = row
                try:
                    val = float(value)

                    # Температура ОЖ
                    if addr == 1039 and val > 90:
                        alerts.append({
                            'severity': 'alarm' if val > 95 else 'warn',
                            'code': 'HIGH_TEMP_ALARM' if val > 95 else 'HIGH_TEMP_WARN',
                            'title': 'КРИТИЧЕСКАЯ ТЕМПЕРАТУРА ОЖ' if val > 95 else 'Высокая температура ОЖ',
                            'details': f'Температура: {val:.1f}°C',
                            'controller_id': ctrl_id,
                            'controller_name': ctrl_name
                        })

                    # Давление масла
                    elif addr == 1038 and val < 2.0:
                        alerts.append({
                            'severity': 'alarm' if val < 1.0 else 'warn',
                            'code': 'LOW_OIL_ALARM' if val < 1.0 else 'LOW_OIL_WARN',
                            'title': 'АВАРИЙНО НИЗКОЕ ДАВЛЕНИЕ МАСЛА' if val < 1.0 else 'Низкое давление масла',
                            'details': f'Давление: {val:.1f} Бар',
                            'controller_id': ctrl_id,
                            'controller_name': ctrl_name
                        })

                    # Напряжение АКБ
                    elif addr == 1036 and (val < 18 or val > 30):
                        alerts.append({
                            'severity': 'alarm' if (val < 16 or val > 32) else 'warn',
                            'code': 'LOW_BATTERY_ALARM' if val < 16 else (
                                'HIGH_BATTERY_ALARM' if val > 32 else 'BATTERY_WARN'),
                            'title': 'КРИТИЧЕСКИ НИЗКОЕ НАПРЯЖЕНИЕ АКБ' if val < 16 else (
                                'КРИТИЧЕСКИ ВЫСОКОЕ НАПРЯЖЕНИЕ АКБ' if val > 32 else 'Отклонение напряжения АКБ'),
                            'details': f'Напряжение: {val:.1f}В',
                            'controller_id': ctrl_id,
                            'controller_name': ctrl_name
                        })

                    # Уровень топлива
                    elif addr == 1040 and val < 20:
                        alerts.append({
                            'severity': 'alarm' if val <= 10 else 'warn',
                            'code': 'LOW_FUEL_ALARM' if val <= 10 else 'LOW_FUEL_WARN',
                            'title': 'КРИТИЧЕСКИ НИЗКИЙ УРОВЕНЬ ТОПЛИВА' if val <= 10 else 'Низкий уровень топлива',
                            'details': f'Уровень топлива: {val:.0f}%',
                            'controller_id': ctrl_id,
                            'controller_name': ctrl_name
                        })

                    # Напряжение сети L1-N (1030) – нижняя граница 207 В
                    elif addr == 1030:
                        if val < 207:
                            alerts.append({
                                'severity': 'warn',
                                'code': 'LOW_VOLT_L1',
                                'title': 'Низкое напряжение сети L1',
                                'details': f'Напряжение L1-N: {val:.1f} В',
                                'controller_id': ctrl_id,
                                'controller_name': ctrl_name
                            })
                        elif val > 253:
                            alerts.append({
                                'severity': 'warn',
                                'code': 'HIGH_VOLT_L1',
                                'title': 'Высокое напряжение сети L1',
                                'details': f'Напряжение L1-N: {val:.1f} В',
                                'controller_id': ctrl_id,
                                'controller_name': ctrl_name
                            })
                    # Аналогично для L2 (1031) и L3 (1032) – если нужно

                    # Частота
                    elif addr == 1029 and (val < 47 or val > 53):
                        alerts.append({
                            'severity': 'alarm' if (val < 47 or val > 53) else 'warn',
                            'code': 'FREQ_ALARM',
                            'title': 'КРИТИЧЕСКОЕ ОТКЛОНЕНИЕ ЧАСТОТЫ',
                            'details': f'Частота: {val:.1f}Гц',
                            'controller_id': ctrl_id,
                            'controller_name': ctrl_name
                        })
                except (ValueError, TypeError):
                    pass

        return alerts

    except Exception as e:
        logger.error(f"Ошибка получения всех аварий: {e}")
        return []


@app.get("/api/parameters/history")
async def get_parameter_history(
        controller_id: Optional[int] = None,
        hours: int = 24,
        db: sqlite3.Connection = Depends(get_db_dependency)
) -> Dict[str, Any]:
    """Возвращает историю ключевых параметров для графиков."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

        parameters = [
            (1000, "RPM", "Обороты"),
            (1039, "temp", "Температура ОЖ"),
            (1038, "oil_pressure", "Давление масла"),
            (1040, "fuel", "Уровень топлива"),
            (1036, "battery", "Напряжение АКБ"),
            (1029, "frequency", "Частота сети"),
        ]

        result = {}

        if controller_id is not None:
            # Одна установка
            for addr, param_key, param_name in parameters:
                # ВАЖНО: убираем фильтр по reason, берем ВСЕ записи
                cursor = db.execute("""
                    SELECT s.ts, s.value_text
                    FROM samples s
                    JOIN register_catalog rc ON rc.id = s.catalog_id
                    WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                    ORDER BY s.ts
                """, (addr, start_ts, controller_id))

                rows = cursor.fetchall()
                result[param_key] = [
                    {
                        "timestamp": datetime.fromtimestamp(r[0]).strftime("%H:%M"),
                        "value": float(r[1]) if r[1] else 0,
                        "ts": r[0]
                    }
                    for r in rows
                ]
        else:
            # Все установки
            cursor = db.execute("SELECT id, name, modbus_name FROM controllers")
            controllers = cursor.fetchall()

            for ctrl in controllers:
                ctrl_id = ctrl['id']
                if ctrl['modbus_name'] and ctrl['modbus_name'].strip():
                    ctrl_name = ctrl['modbus_name']
                else:
                    ctrl_name = ctrl['name']

                for addr, param_key, param_name in parameters:
                    key = f"{param_key}_ctrl_{ctrl_id}"
                    if key not in result:
                        result[key] = {
                            "name": ctrl_name,
                            "controller_id": ctrl_id,
                            "data": []
                        }

                    # ВАЖНО: убираем фильтр по reason, берем ВСЕ записи
                    cursor2 = db.execute("""
                        SELECT s.ts, s.value_text
                        FROM samples s
                        JOIN register_catalog rc ON rc.id = s.catalog_id
                        WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                        ORDER BY s.ts
                    """, (addr, start_ts, ctrl_id))

                    rows = cursor2.fetchall()
                    result[key]["data"] = [
                        {
                            "timestamp": datetime.fromtimestamp(r[0]).strftime("%H:%M"),
                            "value": float(r[1]) if r[1] else 0,
                            "ts": r[0]
                        }
                        for r in rows
                    ]

        return result
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории параметров: {e}")
        return {}


@app.get("/api/export/events")
async def export_events(
        controller_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        format: str = "csv",
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Экспорт журнала событий в CSV или Excel"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        query = """
            SELECT 
                e.id,
                e.ts,
                e.severity,
                e.code,
                e.title,
                e.details,
                c.name,
                c.modbus_name,
                c.ip
            FROM events e
            JOIN controllers c ON c.id = e.controller_id
            WHERE 1=1
        """
        params = []

        if controller_id:
            query += " AND e.controller_id = ?"
            params.append(controller_id)

        if start_date:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
            query += " AND e.ts >= ?"
            params.append(start_ts)

        if end_date:
            end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400
            query += " AND e.ts <= ?"
            params.append(end_ts)

        query += " ORDER BY e.ts DESC"

        cursor = db.execute(query, params)
        rows = cursor.fetchall()

        export_data = []
        for row in rows:
            dt = datetime.fromtimestamp(row['ts']) + timedelta(hours=3)

            # Формируем имя установки: modbus_name > name > ip > ID
            if row['modbus_name'] and row['modbus_name'].strip():
                controller_display = row['modbus_name']
            elif row['name'] and row['name'].strip():
                controller_display = row['name']
            elif row['ip'] and row['ip'].strip():
                controller_display = f"ДГУ ({row['ip']})"
            else:
                controller_display = f"ДГУ {controller_id if controller_id else '?'}"

            export_data.append({
                'Дата и время': dt.strftime("%d.%m.%Y %H:%M:%S"),
                'Установка': controller_display,
                'Важность': row['severity'].upper(),
                'Код': row['code'] or '',
                'Событие': row['title'],
                'Детали': row['details'] or ''
            })

        if format == "xlsx":
            return export_to_excel(export_data, f"events_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        else:
            return export_to_csv(export_data, f"events_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

    except Exception as e:
        logger.error(f"Ошибка экспорта событий: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/export/charts")
async def export_charts_data(
        controller_id: Optional[int] = None,  # теперь может быть None (все установки)
        hours: int = 24,
        format: str = "csv",
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Экспорт данных для графиков по выбранной установке или всем установкам"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

        parameters = [
            (1000, "Обороты (RPM)", "об/мин", "rpm"),
            (1039, "Температура ОЖ", "°C", "temp"),
            (1038, "Давление масла", "Бар", "oil_pressure"),
            (1040, "Уровень топлива", "%", "fuel"),
            (1036, "Напряжение АКБ", "В", "battery"),
            (1029, "Частота сети", "Гц", "frequency"),
        ]

        if format == "xlsx":
            # Создаем Excel с графиками
            wb = Workbook()

            if controller_id:
                # Одна установка
                cursor = db.execute("SELECT name FROM controllers WHERE id = ?", (controller_id,))
                controller = cursor.fetchone()
                controller_name = controller['name'] if controller else f"Установка {controller_id}"

                for addr, param_name, unit, param_key in parameters:
                    cursor = db.execute("""
                        SELECT s.ts, s.value_text
                        FROM samples s
                        JOIN register_catalog rc ON rc.id = s.catalog_id
                        WHERE rc.addr = ? AND s.ts > ?
                        ORDER BY s.ts
                    """, (addr, start_ts))

                    rows = cursor.fetchall()
                    if rows:
                        ws = wb.create_sheet(param_name)

                        # Заголовки
                        ws.cell(row=1, column=1, value="Время")
                        ws.cell(row=1, column=2, value=param_name)
                        ws.cell(row=1, column=3, value=unit)

                        # Данные
                        for i, row in enumerate(rows, 2):
                            dt = datetime.fromtimestamp(row[0]) + timedelta(hours=3)
                            ws.cell(row=i, column=1, value=dt.strftime("%d.%m.%Y %H:%M:%S"))
                            try:
                                value = float(row[1]) if row[1] else 0
                                ws.cell(row=i, column=2, value=value)
                            except:
                                ws.cell(row=i, column=2, value=row[1])

                        # Создаем график
                        from openpyxl.chart import LineChart, Reference
                        chart = LineChart()
                        chart.title = f"{param_name} за последние {hours} часов"
                        chart.y_axis.title = unit
                        chart.x_axis.title = "Время"

                        data = Reference(ws, min_col=2, min_row=1, max_row=len(rows) + 1, max_col=2)
                        cats = Reference(ws, min_col=1, min_row=2, max_row=len(rows) + 1)
                        chart.add_data(data, titles_from_data=True)
                        chart.set_categories(cats)
                        chart.width = 20
                        chart.height = 12

                        ws.add_chart(chart, "E2")
            else:
                # Все установки
                cursor = db.execute("SELECT id, name, modbus_name FROM controllers")
                controllers = cursor.fetchall()

                # Создаем лист для каждого параметра со всеми установками
                for addr, param_name, unit, param_key in parameters:
                    ws = wb.create_sheet(param_name)

                    # Заголовки: Время | Установка1 | Установка2 | ...
                    ws.cell(row=1, column=1, value="Время")
                    col = 2
                    controllers_data = {}
                    all_timestamps = set()

                    for ctrl in controllers:
                        ctrl_id = ctrl['id']
                        if ctrl['modbus_name'] and ctrl['modbus_name'].strip():
                            ctrl_name = ctrl['modbus_name']
                        else:
                            ctrl_name = ctrl['name']

                        ws.cell(row=1, column=col, value=ctrl_name)
                        controllers_data[ctrl_id] = {"name": ctrl_name, "col": col, "data": {}}

                        cursor = db.execute("""
                            SELECT s.ts, s.value_text
                            FROM samples s
                            JOIN register_catalog rc ON rc.id = s.catalog_id
                            WHERE rc.addr = ? AND s.ts > ? AND s.controller_id = ?
                            ORDER BY s.ts
                        """, (addr, start_ts, ctrl_id))

                        rows = cursor.fetchall()
                        for row in rows:
                            dt = datetime.fromtimestamp(row[0]) + timedelta(hours=3)
                            ts_str = dt.strftime("%d.%m.%Y %H:%M:%S")
                            all_timestamps.add(ts_str)
                            try:
                                value = float(row[1]) if row[1] else 0
                                controllers_data[ctrl_id]["data"][ts_str] = value
                            except:
                                controllers_data[ctrl_id]["data"][ts_str] = row[1]

                        col += 1

                    # Заполняем данные
                    sorted_timestamps = sorted(all_timestamps)
                    for row_idx, ts in enumerate(sorted_timestamps, 2):
                        ws.cell(row=row_idx, column=1, value=ts)
                        for ctrl_id, ctrl_info in controllers_data.items():
                            value = ctrl_info["data"].get(ts, "—")
                            ws.cell(row=row_idx, column=ctrl_info["col"], value=value)

                    # Создаем график со всеми линиями
                    if len(controllers_data) > 0:
                        from openpyxl.chart import LineChart, Reference
                        chart = LineChart()
                        chart.title = f"{param_name} - сравнение установок за последние {hours} часов"
                        chart.y_axis.title = unit
                        chart.x_axis.title = "Время"

                        # Добавляем все серии данных
                        for i, (ctrl_id, ctrl_info) in enumerate(controllers_data.items()):
                            data = Reference(ws, min_col=ctrl_info["col"], min_row=1,
                                             max_row=len(sorted_timestamps) + 1, max_col=ctrl_info["col"])
                            chart.add_data(data, titles_from_data=True)

                        cats = Reference(ws, min_col=1, min_row=2, max_row=len(sorted_timestamps) + 1)
                        chart.set_categories(cats)
                        chart.width = 22
                        chart.height = 14

                        ws.add_chart(chart, f"E{len(controllers_data) + 5}")

            # Удаляем дефолтный лист
            if 'Sheet' in wb.sheetnames:
                wb.remove(wb['Sheet'])

            filename = f"charts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)

            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        else:
            # CSV формат - только данные
            # ... существующий код для CSV ...
            pass

    except Exception as e:
        logger.error(f"Ошибка экспорта графиков: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/export/complete-report/{controller_id}")
async def export_complete_report(
        controller_id: int,
        hours: int = 24,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Экспорт полного отчета в Excel с несколькими листами"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        wb = Workbook()

        cursor = db.execute("SELECT name FROM controllers WHERE id = ?", (controller_id,))
        controller = cursor.fetchone()
        controller_name = controller['name'] if controller else f"Установка {controller_id}"

        ws_events = wb.active
        ws_events.title = "Журнал событий"
        await add_events_sheet(ws_events, controller_id, hours, db)

        ws_charts = wb.create_sheet("Данные графиков")
        await add_charts_sheet(ws_charts, controller_id, hours, db)

        ws_status = wb.create_sheet("Текущий статус")
        await add_status_sheet(ws_status, controller_id, db)

        ws_stats = wb.create_sheet("Статистика")
        await add_stats_sheet(ws_stats, controller_id, hours, db)

        filename = f"report_{controller_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error(f"Ошибка создания полного отчета: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/check")
async def debug_check(db: sqlite3.Connection = Depends(get_db_dependency)):
    """Проверка работы API"""
    if db is None:
        return {"error": "DB not connected"}

    try:
        cursor = db.execute("SELECT COUNT(*) as count FROM events")
        events_count = cursor.fetchone()['count']

        cursor = db.execute("""
            SELECT id, ts, code, title FROM events 
            ORDER BY ts DESC LIMIT 5
        """)
        last_events = [dict(row) for row in cursor.fetchall()]

        cursor = db.execute("SELECT COUNT(*) as count FROM last_value")
        last_value_count = cursor.fetchone()['count']

        return {
            "status": "ok",
            "db_path": str(DB_PATH),
            "events_count": events_count,
            "last_value_count": last_value_count,
            "last_events": last_events
        }
    except Exception as e:
        return {"error": str(e)}



# Вспомогательные функции для экспорта

def export_to_csv(data: List[dict], filename: str) -> StreamingResponse:
    """Экспорт данных в CSV"""
    if not data:
        data = [{"Сообщение": "Нет данных для экспорта"}]

    output = io.StringIO()
    fieldnames = data[0].keys() if data else []
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=';')
    writer.writeheader()
    writer.writerows(data)

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue().encode('utf-8-sig')]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
    )


def export_to_excel(data: List[dict], filename: str) -> StreamingResponse:
    """Экспорт данных в Excel"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Экспорт"

    if data:
        headers = list(data[0].keys())
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")

        for row_idx, row in enumerate(data, 2):
            for col_idx, header in enumerate(headers, 1):
                value = row.get(header, "")
                ws.cell(row=row_idx, column=col_idx, value=value)

        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
    )


async def add_events_sheet(ws, controller_id: int, hours: int, db: sqlite3.Connection):
    """Добавление листа с событиями"""
    start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

    cursor = db.execute("""
        SELECT 
            e.ts, e.severity, e.code, e.title, e.details
        FROM events e
        WHERE e.controller_id = ? AND e.ts > ?
        ORDER BY e.ts DESC
    """, (controller_id, start_ts))

    rows = cursor.fetchall()

    headers = ["Дата и время", "Важность", "Код", "Событие", "Детали"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        cell.font = Font(color="FFFFFF", bold=True)

    for row_idx, row in enumerate(rows, 2):
        dt = datetime.fromtimestamp(row['ts']) + timedelta(hours=3)
        ws.cell(row=row_idx, column=1, value=dt.strftime("%d.%m.%Y %H:%M:%S"))

        severity_cell = ws.cell(row=row_idx, column=2, value=row['severity'].upper())
        if row['severity'] == 'alarm':
            severity_cell.fill = PatternFill(start_color="FF0000", end_color="FF0000", fill_type="solid")
            severity_cell.font = Font(color="FFFFFF", bold=True)
        elif row['severity'] == 'warn':
            severity_cell.fill = PatternFill(start_color="FFA500", end_color="FFA500", fill_type="solid")
            severity_cell.font = Font(color="FFFFFF", bold=True)

        ws.cell(row=row_idx, column=3, value=row['code'] or '')
        ws.cell(row=row_idx, column=4, value=row['title'])
        ws.cell(row=row_idx, column=5, value=row['details'] or '')


async def add_charts_sheet(ws, controller_id: int, hours: int, db: sqlite3.Connection):
    """Добавление листа с данными графиков"""
    start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

    parameters = [
        (1000, "Обороты (RPM)", "об/мин"),
        (1039, "Температура ОЖ", "°C"),
        (1038, "Давление масла", "Бар"),
        (1040, "Уровень топлива", "%"),
        (1036, "Напряжение АКБ", "В"),
        (1029, "Частота сети", "Гц"),
    ]

    ws.cell(row=1, column=1, value="Дата и время")
    for col, (_, name, unit) in enumerate(parameters, 2):
        ws.cell(row=1, column=col, value=f"{name}, {unit}")
        ws.cell(row=1, column=col).font = Font(bold=True)

    all_data = {}
    timestamps = set()

    for idx, (addr, _, _) in enumerate(parameters):
        cursor = db.execute("""
            SELECT s.ts, s.value_text
            FROM samples s
            JOIN register_catalog rc ON rc.id = s.catalog_id
            WHERE rc.addr = ? AND s.ts > ?
            ORDER BY s.ts
        """, (addr, start_ts))

        rows = cursor.fetchall()
        for row in rows:
            dt = datetime.fromtimestamp(row['ts']) + timedelta(hours=3)
            ts_str = dt.strftime("%d.%m.%Y %H:%M:%S")
            timestamps.add(ts_str)
            try:
                value = float(row[1]) if row[1] else None
                all_data[(ts_str, idx)] = f"{value:.1f}" if value is not None else "—"
            except:
                all_data[(ts_str, idx)] = "—"

    sorted_timestamps = sorted(timestamps)
    for row_idx, ts in enumerate(sorted_timestamps, 2):
        ws.cell(row=row_idx, column=1, value=ts)
        for col_idx in range(len(parameters)):
            value = all_data.get((ts, col_idx), "—")
            ws.cell(row=row_idx, column=col_idx + 2, value=value)


async def add_status_sheet(ws, controller_id: int, db: sqlite3.Connection):
    """Добавление листа с текущим статусом"""
    ws.cell(row=1, column=1, value="Параметр")
    ws.cell(row=1, column=2, value="Значение")
    ws.cell(row=1, column=3, value="Единица")
    ws.cell(row=1, column=1).font = Font(bold=True)
    ws.cell(row=1, column=2).font = Font(bold=True)
    ws.cell(row=1, column=3).font = Font(bold=True)

    status_params = [
        (1000, "Обороты", "об/мин"),
        (1039, "Температура ОЖ", "°C"),
        (1038, "Давление масла", "Бар"),
        (1040, "Уровень топлива", "%"),
        (1036, "Напряжение АКБ", "В"),
        (1029, "Частота сети", "Гц"),
        (1017, "Частота генератора", "Гц"),
        (1018, "Напряжение L1-N", "В"),
        (1019, "Напряжение L2-N", "В"),
        (1020, "Напряжение L3-N", "В"),
    ]

    row = 2
    for addr, name, unit in status_params:
        cursor = db.execute("""
            SELECT lv.last_value_text
            FROM last_value lv
            JOIN register_catalog rc ON rc.id = lv.catalog_id
            WHERE rc.addr = ?
        """, (addr,))
        result = cursor.fetchone()
        value = result[0] if result and result[0] else "—"

        ws.cell(row=row, column=1, value=name)
        ws.cell(row=row, column=2, value=value)
        ws.cell(row=row, column=3, value=unit)
        row += 1


async def add_stats_sheet(ws, controller_id: int, hours: int, db: sqlite3.Connection):
    """Добавление листа со статистикой"""
    start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

    cursor = db.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN severity = 'alarm' THEN 1 ELSE 0 END) as alarms,
            SUM(CASE WHEN severity = 'warn' THEN 1 ELSE 0 END) as warns
        FROM events
        WHERE controller_id = ? AND ts > ?
    """, (controller_id, start_ts))

    stats = cursor.fetchone()

    ws.cell(row=1, column=1, value="Показатель")
    ws.cell(row=1, column=2, value="Значение")
    ws.cell(row=1, column=1).font = Font(bold=True)
    ws.cell(row=1, column=2).font = Font(bold=True)

    ws.cell(row=2, column=1, value="Всего событий")
    ws.cell(row=2, column=2, value=stats['total'] or 0)

    ws.cell(row=3, column=1, value="Аварий (alarm)")
    ws.cell(row=3, column=2, value=stats['alarms'] or 0)

    ws.cell(row=4, column=1, value="Предупреждений (warn)")
    ws.cell(row=4, column=2, value=stats['warns'] or 0)

    ws.cell(row=6, column=1, value="Период отчета")
    ws.cell(row=6, column=2, value=f"Последние {hours} часов")

    ws.cell(row=7, column=1, value="Дата формирования")
    ws.cell(row=7, column=2, value=datetime.now().strftime("%d.%m.%Y %H:%M:%S"))


# Запуск приложения
if __name__ == "__main__":
    import uvicorn

    logger.info(f"🚀 Запуск сервера на http://localhost:8000")
    logger.info(f"📁 Папка с шаблонами: {TEMPLATES_DIR}")
    logger.info(f"📁 Папка со статикой: {STATIC_DIR}")
    logger.info(f"📁 База данных: {DB_PATH or 'НЕ НАЙДЕНА'}")
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ============================================================
# LLM АГЕНТ
# ============================================================

# Добавляем путь к llm_agent
LLM_AGENT_PATH = Path(__file__).parent.parent / "llm_agent"
if str(LLM_AGENT_PATH) not in sys.path:
    sys.path.insert(0, str(LLM_AGENT_PATH))

# Глобальный экземпляр агента
llm_agent = None


def init_llm_agent():
    """Инициализация LLM агента при старте сервера"""
    global llm_agent
    try:
        from simple_agent import DieselLLMAgent
        print("🔄 Инициализация LLM агента...")
        llm_agent = DieselLLMAgent(auto_index=True)
        print("✅ LLM агент готов")
        return True
    except ImportError as e:
        print(f"❌ Не удалось импортировать LLM агент: {e}")
        print("   Убедитесь, что установлены зависимости: pip install sentence-transformers chromadb openpyxl")
        return False
    except Exception as e:
        print(f"❌ Ошибка инициализации LLM агента: {e}")
        return False


@app.on_event("startup")
async def startup_llm():
    """Запуск LLM агента при старте сервера"""
    init_llm_agent()


@app.get("/api/llm/status")
async def llm_status():
    """Проверка статуса LLM агента"""
    if llm_agent is None:
        return {"status": "not_initialized", "message": "LLM агент не инициализирован"}
    try:
        stats = llm_agent.get_stats()
        return {"status": "ready", "stats": stats}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/llm/ask")
async def llm_ask(request: dict):
    """Запрос к LLM агенту"""
    if llm_agent is None:
        raise HTTPException(status_code=503, detail="LLM агент не готов")

    question = request.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Вопрос не может быть пустым")

    monitoring_data = request.get("monitoring_data", {})

    try:
        if monitoring_data:
            result = llm_agent.ask_with_status(question, monitoring_data)
        else:
            result = llm_agent.ask(question)

        return {
            "success": True,
            "answer": result["answer"],
            "sources": result.get("sources", []),
            "question": question
        }
    except Exception as e:
        logger.error(f"LLM ошибка: {e}")
        return {
            "success": False,
            "answer": f"Извините, произошла ошибка: {str(e)}",
            "sources": [],
            "question": question
        }


@app.get("/api/llm/sources")
async def llm_sources():
    """Получить список загруженных источников документов"""
    if llm_agent is None:
        raise HTTPException(status_code=503, detail="LLM агент не готов")

    try:
        sources = llm_agent.get_available_sources()
        return {"sources": sources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/llm/refresh")
async def llm_refresh():
    """Принудительное обновление индекса документов"""
    if llm_agent is None:
        raise HTTPException(status_code=503, detail="LLM агент не готов")

    try:
        result = llm_agent.refresh_documents()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))