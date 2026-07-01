# add_user.py
import sqlite3
import hashlib
import secrets


def generate_password_hash(password: str, salt: str = None) -> tuple:
    """Генерирует хеш и соль для пароля"""
    if salt is None:
        salt = secrets.token_hex(16)
    hash_obj = hashlib.sha256()
    hash_obj.update((password + salt).encode('utf-8'))
    return hash_obj.hexdigest(), salt


def add_user():
    db_path = "diesel.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("\n" + "=" * 60)
    print("👤 ДОБАВЛЕНИЕ НОВОГО ПОЛЬЗОВАТЕЛЯ")
    print("=" * 60)

    # Запрашиваем данные
    username = input("👤 Введите логин: ").strip()
    if not username:
        print("❌ Логин не может быть пустым")
        return

    password = input("🔑 Введите пароль: ").strip()
    if len(password) < 4:
        print("❌ Пароль должен быть не менее 4 символов")
        return

    is_admin = input("👑 Дать права администратора? (y/n): ").strip().lower() == 'y'

    # Спрашиваем про установки
    controllers_input = input("📌 ID установок через запятую (например: 1,2,3): ").strip()
    controller_ids = []
    if controllers_input:
        try:
            controller_ids = [int(x.strip()) for x in controllers_input.split(',') if x.strip()]
        except ValueError:
            print("⚠️ Некорректный ввод установок, пропускаем")

    try:
        # Проверяем, не занят ли логин
        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            print(f"❌ Пользователь '{username}' уже существует")
            return

        # Генерируем хеш
        hash_val, salt = generate_password_hash(password)

        # Создаем пользователя
        cursor = conn.execute("""
            INSERT INTO users (username, password_hash, salt, is_admin, created_ts) 
            VALUES (?, ?, ?, ?, strftime('%s', 'now'))
        """, (username, hash_val, salt, 1 if is_admin else 0))

        user_id = cursor.lastrowid

        # Добавляем доступ к установкам
        for ctrl_id in controller_ids:
            conn.execute("""
                INSERT OR IGNORE INTO user_controllers (user_id, controller_id) 
                VALUES (?, ?)
            """, (user_id, ctrl_id))

        conn.commit()

        print("\n" + "=" * 60)
        print("✅ ПОЛЬЗОВАТЕЛЬ СОЗДАН!")
        print("=" * 60)
        print(f"👤 Логин: {username}")
        print(f"🔑 Пароль: {password}")
        print(f"👑 Админ: {'Да' if is_admin else 'Нет'}")
        if controller_ids:
            print(f"📌 Доступ к установкам: {controller_ids}")
        else:
            print("📌 Доступ к установкам: не указан")
        print("=" * 60)
        print("\n📝 Данные для SQL (на всякий случай):")
        print(f"Хеш: {hash_val}")
        print(f"Соль: {salt}")

    except sqlite3.Error as e:
        print(f"❌ Ошибка: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    add_user()