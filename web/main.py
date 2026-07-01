# app/web/main.py
"""
Веб-сервер для мониторинга ДГУ
"""

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
import os
from pathlib import Path
import logging
import re
import sys
import time

# ===== ВЫНЕСЕННЫЕ МОДУЛИ =====
from .database import get_db_dependency, DB_PATH, get_controller_display_name
from .status_service import StatusService
from .event_service import EventService
from .chart_service import ChartService
from .llm_service import LLMService

# ===== НАСТРОЙКА =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "js").mkdir(exist_ok=True)
(STATIC_DIR / "images").mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Diesel Monitoring Web")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ===== ИНИЦИАЛИЗАЦИЯ СЕРВИСОВ =====
event_service = EventService(DB_PATH)
status_service = StatusService(DB_PATH)
chart_service = ChartService(DB_PATH)
llm_service = LLMService(DB_PATH)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def clean_signal_name(name: str) -> str:
    """Убирает цифры в начале названия сигнала"""
    if not name:
        return name
    cleaned = re.sub(r'^\d+\s+', '', str(name))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def get_severity_info(code: str, title: str, details: str = "") -> dict:
    """Определяет severity и иконку для события"""
    code = (code or '').upper()
    title_lower = (title or '').lower()

    if code in ('BIT_2',):
        if 'ВЫКЛ' in (details or ''):
            return {'severity': 'success', 'icon': '✅'}
        return {'severity': 'alarm', 'icon': '🚨'}

    if code in ('BIT_230', 'BIT_257'):
        if 'ВЫКЛ' in (details or ''):
            return {'severity': 'success', 'icon': '✅'}
        return {'severity': 'warn', 'icon': '⚠️'}
    elif any(k in code for k in ['NORMAL', 'OK', 'RESTORED', 'RECOVERED', 'FREQ_NORMAL', 'MAINS_OK']):
        return {'severity': 'success', 'icon': '✅'}
    elif any(k in title_lower for k in ['авар', 'критич', 'alarm', 'ост', 'shutdown', 'стоп', 'рво', 'пожар']):
        return {'severity': 'alarm', 'icon': '🚨'}
    elif any(k in title_lower for k in ['прд', 'пред', 'warn', 'низк', 'высок']):
        return {'severity': 'warn', 'icon': '⚠️'}
    return {'severity': 'info', 'icon': 'ℹ️'}


def get_bit_status(addr: int, value: Any) -> dict:
    """Получает статус бита"""
    state = "ВКЛ" if value == '1' else "ВЫКЛ"
    return {"state": state, "state_num": int(value or 0)}


# ==================== ЭНДПОИНТЫ ====================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "title": "Diesel Monitoring", "now": datetime.now()}
    )


@app.get("/api/controllers")
async def get_controllers(db: sqlite3.Connection = Depends(get_db_dependency)):
    """Возвращает список всех контроллеров"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        cursor = db.execute("SELECT id, name, ip, port, unit_id FROM controllers ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка получения контроллеров: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/controller-name/{controller_id}")
async def get_controller_name(controller_id: int, db: sqlite3.Connection = Depends(get_db_dependency)):
    """Возвращает имя контроллера"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        cursor = db.execute("SELECT name, modbus_name, ip FROM controllers WHERE id = ?", (controller_id,))
        row = cursor.fetchone()
        if not row:
            return {"name": f"ДГУ {controller_id}", "ip": "", "id": controller_id}

        display_name = row['modbus_name'] if row['modbus_name'] and row['modbus_name'].strip() else row['name']
        return {"name": display_name, "ip": row['ip'] or "", "id": controller_id}
    except Exception as e:
        logger.error(f"Ошибка получения имени контроллера: {e}")
        return {"name": f"ДГУ {controller_id}", "ip": "", "id": controller_id}


@app.get("/api/notifications")
async def get_notifications(
        controller_id: Optional[int] = None,
        date: Optional[str] = None,
        limit: int = 150,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Возвращает уведомления с filter_passed=1"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await event_service.get_notifications(db, controller_id, date, limit)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    # Добавляем информацию о severity
    for item in result:
        severity_info = get_severity_info(item.get('code'), item.get('title'), item.get('details'))
        item.update(severity_info)
        item['controller_display'] = get_controller_display_name(item)

    return result


@app.get("/api/bit-info/{addr}")
async def get_bit_info(addr: int, db: sqlite3.Connection = Depends(get_db_dependency)):
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
        return {"addr": addr, "name": f"BIT_{addr}", "group_name": "", "priority": 1}
    except Exception as e:
        logger.error(f"Ошибка получения информации о бите: {e}")
        return {"addr": addr, "name": f"BIT_{addr}", "group_name": "", "priority": 1}


@app.get("/api/status-snapshot/{notif_id}")
async def get_status_snapshot(notif_id: int, db: sqlite3.Connection = Depends(get_db_dependency)):
    """Возвращает снимок статуса на момент события"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await status_service.get_snapshot(db, notif_id)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/status/{controller_id}")
async def get_full_status(controller_id: int, db: sqlite3.Connection = Depends(get_db_dependency)):
    """Возвращает полный текущий статус установки"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await status_service.get_full_status(db, controller_id)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/current-bits-status")
async def get_current_bits_status(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Возвращает текущее состояние важных битов"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await status_service.get_bits_status(db, controller_id)
    if "error" in result:
        return {"status": "error", "bits": {}, "timestamp": 0}
    return result


@app.get("/api/current-alerts")
async def get_all_current_alerts(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Возвращает активные аварии и предупреждения"""
    if db is None:
        return []

    result = await status_service.get_current_alerts(db, controller_id)
    if "error" in result:
        return []
    return result


@app.get("/api/parameters/history")
async def get_parameter_history(
        controller_id: Optional[int] = None,
        hours: int = 24,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Возвращает историю ключевых параметров для графиков"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await chart_service.get_history(db, controller_id, hours)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


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

    result = await event_service.export_events(db, controller_id, start_date, end_date, format)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return StreamingResponse(
        iter([result["content"]]),
        media_type=result["media_type"],
        headers={"Content-Disposition": f"attachment; filename={result['filename']}"}
    )


@app.get("/api/export/charts")
async def export_charts_data(
        controller_id: Optional[int] = None,
        hours: int = 24,
        format: str = "csv",
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Экспорт данных для графиков"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await chart_service.export_charts(db, controller_id, hours, format)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return StreamingResponse(
        iter([result["content"]]),
        media_type=result["media_type"],
        headers={"Content-Disposition": f"attachment; filename={result['filename']}"}
    )


@app.get("/api/export/complete-report/{controller_id}")
async def export_complete_report(
        controller_id: int,
        hours: int = 24,
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Экспорт полного отчета в Excel"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await status_service.export_report(db, controller_id, hours)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return StreamingResponse(
        iter([result["content"]]),
        media_type=result["media_type"],
        headers={"Content-Disposition": f"attachment; filename={result['filename']}"}
    )


@app.get("/api/debug/check")
async def debug_check(db: sqlite3.Connection = Depends(get_db_dependency)):
    """Проверка работы API"""
    if db is None:
        return {"error": "DB not connected"}
    try:
        cursor = db.execute("SELECT COUNT(*) as count FROM events")
        events_count = cursor.fetchone()['count']
        cursor = db.execute("SELECT id, ts, code, title FROM events ORDER BY ts DESC LIMIT 5")
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


# Добавьте после других эндпоинтов в app/web/main.py

@app.get("/api/download-template/{filename}")
async def download_template(filename: str):
    """Скачивание готового шаблона из папки templates"""
    # Путь к папке с шаблонами
    templates_dir = BASE_DIR / "templates" / "documents"

    # Проверка безопасности - разрешаем только .docx файлы
    if not filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="Недопустимый тип файла")

    # Защита от path traversal
    safe_filename = Path(filename).name
    file_path = templates_dir / safe_filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Файл {filename} не найден")

    # Возвращаем файл для скачивания
    return FileResponse(
        path=str(file_path),
        filename=safe_filename,
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )


@app.get("/api/list-templates")
async def list_templates():
    """Получить список доступных шаблонов"""
    templates_dir = BASE_DIR / "templates" / "documents"
    templates_dir.mkdir(exist_ok=True)

    files = []
    for file in templates_dir.glob("*.docx"):
        files.append({
            "name": file.name,
            "size": file.stat().st_size,
            "modified": datetime.fromtimestamp(file.stat().st_mtime).isoformat()
        })

    # Сортировка по имени
    files.sort(key=lambda x: x['name'])
    return files


# ==================== LLM АГЕНТ ====================

@app.on_event("startup")
async def startup_llm():
    """Запуск LLM агента при старте сервера"""
    llm_service.init_agent()


@app.get("/api/llm/status")
async def llm_status():
    """Проверка статуса LLM агента"""
    return llm_service.get_status()


@app.post("/api/llm/ask")
async def llm_ask(request: dict):
    """Запрос к LLM агенту"""
    return await llm_service.ask(request)


@app.get("/api/llm/sources")
async def llm_sources():
    """Получить список загруженных источников документов"""
    return await llm_service.get_sources()


@app.post("/api/llm/refresh")
async def llm_refresh():
    """Принудительное обновление индекса документов"""
    return await llm_service.refresh()


# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    import uvicorn

    logger.info(f"🚀 Запуск сервера на http://localhost:8000")
    logger.info(f"📁 База данных: {DB_PATH or 'НЕ НАЙДЕНА'}")
    uvicorn.run(app, host="0.0.0.0", port=8000)