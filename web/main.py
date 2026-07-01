# app/web/main.py
"""
Веб-сервер для мониторинга ДГУ с аутентификацией
"""

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, RedirectResponse
import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import os
from pathlib import Path
import logging
import re
import sys
import time
import hashlib

# ===== ВЫНЕСЕННЫЕ МОДУЛИ =====
from .database import get_db_dependency, DB_PATH, get_controller_display_name
from .status_service import StatusService
from .event_service import EventService
from .chart_service import ChartService
from .llm_service import LLMService
from .auth import (
    get_current_user, require_auth, require_admin,
    get_user_controllers, filter_controllers_by_user,
    create_session_token, decode_session_token,
    get_encryption_key
)

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


# ==================== СТРАНИЦА ВХОДА ====================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа"""
    db = next(get_db_dependency())
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None}
    )


@app.post("/login")
async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Обработка входа"""
    if db is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "База данных недоступна"}
        )

    try:
        # ===== ОТЛАДКА: выводим полученные данные =====
        print("\n" + "=" * 60)
        print("🔍 ПОПЫТКА ВХОДА")
        print("=" * 60)
        print(f"👤 Введенный логин: '{username}'")
        print(f"🔑 Введенный пароль: '{password}'")
        print("=" * 60)

        cursor = db.execute(
            "SELECT id, username, password_hash, salt, is_admin FROM users WHERE username = ?",
            (username,)
        )
        user = cursor.fetchone()

        if not user:
            print(f"❌ Пользователь '{username}' не найден в БД")
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Неверное имя пользователя или пароль"}
            )

        print(f"\n📋 Данные пользователя из БД:")
        print(f"   ID: {user['id']}")
        print(f"   Username: {user['username']}")
        print(f"   Хеш из БД: {user['password_hash']}")
        print(f"   Соль из БД: {user['salt']}")
        print(f"   Is Admin: {user['is_admin']}")

        # Вычисляем хеш от введенного пароля
        hash_obj = hashlib.sha256()
        hash_obj.update((password + user["salt"]).encode('utf-8'))
        computed_hash = hash_obj.hexdigest()

        print(f"\n🔐 Вычисленный хеш: {computed_hash}")
        print(f"📊 Хеши совпадают: {computed_hash == user['password_hash']}")
        print("=" * 60)

        if computed_hash != user["password_hash"]:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Неверное имя пользователя или пароль"}
            )

        # Обновляем время последнего входа
        db.execute(
            "UPDATE users SET last_login_ts = strftime('%s', 'now') WHERE id = ?",
            (user["id"],)
        )
        db.commit()

        # Создаем сессию
        session_token = create_session_token(user["id"], user["username"])
        print(f"✅ Создан токен сессии для {user['username']}")
        print("=" * 60 + "\n")

        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            max_age=24 * 60 * 60,
            path="/"
        )
        return response

    except sqlite3.Error as e:
        print(f"❌ Ошибка SQL: {e}")
        logger.error(f"Ошибка входа: {e}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ошибка при входе"}
        )
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Внутренняя ошибка сервера"}
        )


@app.get("/logout")
async def logout():
    """Выход из системы"""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token", path="/")
    return response


# ==================== ЗАЩИЩЕННАЯ ГЛАВНАЯ ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Перенаправление на страницу входа или дашборд"""
    db = next(get_db_dependency())
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
        request: Request,
        user: dict = Depends(require_auth),
        db: sqlite3.Connection = Depends(get_db_dependency)
):
    """Основная страница (требуется авторизация)"""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "Diesel Monitoring",
            "now": datetime.now(),
            "user": user
        }
    )


# ==================== API ЭНДПОИНТЫ ====================

@app.get("/api/controllers")
async def get_controllers(
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает список контроллеров, доступных пользователю"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        cursor = db.execute("SELECT id, name, ip, port, unit_id FROM controllers ORDER BY name")
        controllers = [dict(row) for row in cursor.fetchall()]
        filtered = filter_controllers_by_user(controllers, user["id"], db)
        return filtered

    except sqlite3.Error as e:
        logger.error(f"❌ Ошибка получения контроллеров: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/controller-name/{controller_id}")
async def get_controller_name(
        controller_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает имя контроллера с проверкой доступа"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

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
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает уведомления с filter_passed=1 с учетом прав доступа"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await event_service.get_notifications(db, controller_id, date, limit)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    if not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        result = [item for item in result if item.get("controller_id") in allowed]

    for item in result:
        severity_info = get_severity_info(item.get('code'), item.get('title'), item.get('details'))
        item.update(severity_info)
        item['controller_display'] = get_controller_display_name(item)

    return result


@app.get("/api/bit-info/{addr}")
async def get_bit_info(
        addr: int,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
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
async def get_status_snapshot(
        notif_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает снимок статуса на момент события"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await status_service.get_snapshot(db, notif_id)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/status/{controller_id}")
async def get_full_status(
        controller_id: int,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает полный текущий статус установки"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await status_service.get_full_status(db, controller_id)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/current-bits-status")
async def get_current_bits_status(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает текущее состояние важных битов"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await status_service.get_bits_status(db, controller_id)
    if "error" in result:
        return {"status": "error", "bits": {}, "timestamp": 0}
    return result


@app.get("/api/current-alerts")
async def get_all_current_alerts(
        controller_id: Optional[int] = None,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает активные аварии и предупреждения"""
    if db is None:
        return []

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await status_service.get_current_alerts(db, controller_id)
    if "error" in result:
        return []

    if not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        result = [item for item in result if item.get("controller_id") in allowed]

    return result


@app.get("/api/parameters/history")
async def get_parameter_history(
        controller_id: Optional[int] = None,
        hours: int = 24,
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Возвращает историю ключевых параметров для графиков"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

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
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Экспорт журнала событий в CSV или Excel"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

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
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Экспорт данных для графиков"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if controller_id and not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

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
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
    """Экспорт полного отчета в Excel"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if not user.get("is_admin", False):
        allowed = set(get_user_controllers(user["id"], db))
        if controller_id not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await status_service.export_report(db, controller_id, hours)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return StreamingResponse(
        iter([result["content"]]),
        media_type=result["media_type"],
        headers={"Content-Disposition": f"attachment; filename={result['filename']}"}
    )


@app.get("/api/debug/check")
async def debug_check(
        db: sqlite3.Connection = Depends(get_db_dependency),
        user: dict = Depends(require_auth)
):
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
            "last_events": last_events,
            "user": user
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/download-template/{filename}")
async def download_template(
        filename: str,
        user: dict = Depends(require_auth)
):
    """Скачивание готового шаблона из папки templates"""
    templates_dir = BASE_DIR / "templates" / "documents"

    if not filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="Недопустимый тип файла")

    safe_filename = Path(filename).name
    file_path = templates_dir / safe_filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Файл {filename} не найден")

    return FileResponse(
        path=str(file_path),
        filename=safe_filename,
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )


@app.get("/api/list-templates")
async def list_templates(
        user: dict = Depends(require_auth)
):
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

    files.sort(key=lambda x: x['name'])
    return files


# ==================== LLM АГЕНТ ====================

@app.on_event("startup")
async def startup_llm():
    """Запуск LLM агента при старте сервера"""
    llm_service.init_agent()


@app.get("/api/llm/status")
async def llm_status(
        user: dict = Depends(require_auth)
):
    """Проверка статуса LLM агента"""
    return llm_service.get_status()


@app.post("/api/llm/ask")
async def llm_ask(
        request: dict,
        user: dict = Depends(require_auth)
):
    """Запрос к LLM агенту"""
    return await llm_service.ask(request)


@app.get("/api/llm/sources")
async def llm_sources(
        user: dict = Depends(require_auth)
):
    """Получить список загруженных источников документов"""
    return await llm_service.get_sources()


@app.post("/api/llm/refresh")
async def llm_refresh(
        user: dict = Depends(require_auth)
):
    """Принудительное обновление индекса документов"""
    return await llm_service.refresh()


# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    import uvicorn

    logger.info(f"🚀 Запуск сервера на http://localhost:8000")
    logger.info(f"📁 База данных: {DB_PATH or 'НЕ НАЙДЕНА'}")
    logger.info("👤 Логин по умолчанию: admin / admin123")
    uvicorn.run(app, host="0.0.0.0", port=8000)