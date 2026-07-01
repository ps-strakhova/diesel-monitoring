# app/web/auth.py
import sqlite3
from typing import Optional, List, Dict, Any
from fastapi import Request, HTTPException, Depends
from datetime import datetime, timedelta
import secrets
import hashlib
import json
import os
from cryptography.fernet import Fernet
from .database import DB_PATH, get_db_dependency


# ===== ГЕНЕРАЦИЯ КЛЮЧА ШИФРОВАНИЯ =====

def get_encryption_key() -> bytes:
    """
    Генерирует или загружает ключ шифрования.
    """
    # Пытаемся загрузить ключ из файла
    key_file = os.path.join(os.path.dirname(DB_PATH), ".encryption_key")

    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()

    # Генерируем новый ключ
    key = Fernet.generate_key()

    # Сохраняем ключ в файл
    with open(key_file, "wb") as f:
        f.write(key)

    # Устанавливаем права доступа только для владельца
    try:
        os.chmod(key_file, 0o600)
    except:
        pass

    return key


# Инициализируем шифровальщик
ENCRYPTION_KEY = get_encryption_key()
cipher = Fernet(ENCRYPTION_KEY)

SESSION_EXPIRY_HOURS = 24


def create_session_token(user_id: int, username: str) -> str:
    """Создает зашифрованный токен сессии"""
    data = {
        "user_id": user_id,
        "username": username,
        "expires": (datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)).isoformat(),
        "created": datetime.now().isoformat()
    }

    json_data = json.dumps(data)
    encrypted = cipher.encrypt(json_data.encode('utf-8'))
    salt = secrets.token_hex(8)
    return salt + ":" + encrypted.decode('utf-8')


def decode_session_token(token: str) -> Optional[Dict]:
    """Декодирует и проверяет токен сессии"""
    try:
        if ":" not in token:
            return None

        _, encrypted = token.split(":", 1)
        decrypted = cipher.decrypt(encrypted.encode('utf-8'))
        data = json.loads(decrypted.decode('utf-8'))

        expires = datetime.fromisoformat(data["expires"])
        if expires < datetime.now():
            return None

        return data
    except Exception:
        return None


def get_current_user(request: Request, db: sqlite3.Connection = Depends(get_db_dependency)) -> Optional[Dict]:
    """Получает текущего пользователя из сессии"""
    token = request.cookies.get("session_token")
    if not token:
        return None

    session_data = decode_session_token(token)
    if not session_data:
        return None

    user_id = session_data.get("user_id")
    if not user_id:
        return None

    try:
        cursor = db.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"])
        }
    except:
        return None


def get_user_controllers(user_id: int, db: sqlite3.Connection) -> List[int]:
    """Получает список ID установок, доступных пользователю"""
    try:
        cursor = db.execute(
            "SELECT controller_id FROM user_controllers WHERE user_id = ?",
            (user_id,)
        )
        return [row["controller_id"] for row in cursor.fetchall()]
    except:
        return []


def filter_controllers_by_user(controllers: List[Dict], user_id: int, db: sqlite3.Connection) -> List[Dict]:
    """Фильтрует список установок по правам пользователя"""
    if not controllers:
        return controllers

    # Проверяем, является ли пользователь администратором
    cursor = db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row["is_admin"]:
        return controllers  # Администратор видит все

    # Получаем доступные установки
    allowed_ids = set(get_user_controllers(user_id, db))
    return [c for c in controllers if c["id"] in allowed_ids]


# ============ DEPENDENCIES ДЛЯ ЗАЩИТЫ ЭНДПОИНТОВ ============

def require_auth(request: Request, db: sqlite3.Connection = Depends(get_db_dependency)):
    """Зависимость для проверки аутентификации"""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Необходима авторизация")
    return user


def require_admin(user: dict = Depends(require_auth)):
    """Зависимость для проверки прав администратора"""
    if not user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user