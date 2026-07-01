#!/usr/bin/env python
"""
УНИВЕРСАЛЬНЫЙ ЗАПУСК СИСТЕМЫ МОНИТОРИНГА ДГУ

Запуск всех компонентов:
  python run.py              - Запуск мониторинга + веб-сервер + LLM агент
  python run.py --monitor    - Только мониторинг
  python run.py --web        - Только веб-сервер
  python run.py --llm        - Только LLM агент (интерактивный режим)
  python run.py --status     - Проверка статуса всех компонентов
  python run.py --init-db    - Инициализация базы данных
"""

import os
import sys
import time
import argparse
from pathlib import Path
import threading
import signal
import atexit

# ============================================================
# НАСТРОЙКИ (ДЛЯ ВАШЕЙ СТРУКТУРЫ)
# ============================================================

BASE_DIR = Path(__file__).parent  # diesel/
APP_DIR = BASE_DIR / "app"  # diesel/app/
WEB_DIR = BASE_DIR / "web"  # diesel/web/
LLM_DIR = BASE_DIR / "llm_agent"  # diesel/llm_agent/

# ДОБАВЛЯЕМ ВСЕ ПУТИ ДЛЯ ИМПОРТОВ
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(WEB_DIR))
sys.path.insert(0, str(LLM_DIR))

# Пути внутри LLM
DOCS_DIR = LLM_DIR / "documents"
CHROMA_DIR = LLM_DIR / "chroma_storage"


# ============================================================
# ЦВЕТА ДЛЯ ВЫВОДА
# ============================================================

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    print(f"\n{Colors.CYAN}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}{text}{Colors.RESET}")
    print(f"{Colors.CYAN}{'=' * 60}{Colors.RESET}")


def print_success(text: str):
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")


def print_error(text: str):
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")


def print_warning(text: str):
    print(f"{Colors.YELLOW}⚠️ {text}{Colors.RESET}")


def print_info(text: str):
    print(f"{Colors.BLUE}ℹ️ {text}{Colors.RESET}")


# ============================================================
# ПРОВЕРКА ОКРУЖЕНИЯ
# ============================================================

def check_environment():
    """Проверка окружения перед запуском"""
    print_header("🔍 ПРОВЕРКА ОКРУЖЕНИЯ")

    # 1. Проверка .env
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        print_success(".env файл найден")
    else:
        print_warning(".env файл не найден! Создайте его")

    # 2. Проверка app/
    if APP_DIR.exists():
        print_success(f"Папка app: {APP_DIR}")
    else:
        print_error(f"❌ Папка app не найдена: {APP_DIR}")

    # 3. Проверка web/
    if WEB_DIR.exists():
        print_success(f"Папка web: {WEB_DIR}")
        web_main = WEB_DIR / "main.py"
        if web_main.exists():
            print_success(f"  ✅ web/main.py найден")
        else:
            print_error(f"  ❌ web/main.py не найден")
    else:
        print_error(f"❌ Папка web не найдена: {WEB_DIR}")

    # 4. Проверка llm_agent/
    if LLM_DIR.exists():
        print_success(f"Папка llm_agent: {LLM_DIR}")
        agent_file = LLM_DIR / "simple_agent.py"
        if agent_file.exists():
            print_success(f"  ✅ simple_agent.py найден")
        else:
            print_error(f"  ❌ simple_agent.py не найден")

        if DOCS_DIR.exists():
            doc_count = len(list(DOCS_DIR.glob("*")))
            if doc_count > 0:
                print_success(f"  ✅ documents: {doc_count} файлов")
                for f in list(DOCS_DIR.glob("*"))[:3]:
                    print_info(f"     - {f.name}")
                if doc_count > 3:
                    print_info(f"     ... и еще {doc_count - 3} файлов")
            else:
                print_warning(f"  ⚠️ documents пуста")
        else:
            print_warning(f"  ⚠️ documents не найдена")

        if CHROMA_DIR.exists():
            print_success(f"  ✅ chroma_storage существует")
        else:
            print_info(f"  ℹ️ chroma_storage будет создана при первом запуске")
    else:
        print_error(f"❌ Папка llm_agent не найдена: {LLM_DIR}")

    # 5. Проверка БД
    db_file = BASE_DIR / os.getenv("DB_PATH", "diesel.db")
    if db_file.exists():
        print_success(f"База данных: {db_file}")
    else:
        print_warning(f"База данных не найдена: {db_file}")
        print_info("Будет создана при первом запуске мониторинга")

    # 6. Проверка cfgmodbus.txt
    cfg_file = BASE_DIR / "data" / "cfgmodbus.txt"
    if cfg_file.exists():
        print_success(f"Файл конфигурации Modbus: {cfg_file}")
    else:
        print_warning(f"Файл конфигурации не найден: {cfg_file}")
        print_info("Поместите cfgmodbus.txt в папку data/")

    print_header("✅ ПРОВЕРКА ЗАВЕРШЕНА")


# ============================================================
# ЗАПУСК КОМПОНЕНТОВ
# ============================================================

def run_monitor():
    """Запуск основного мониторинга"""
    print_header("🔄 ЗАПУСК МОНИТОРИНГА")
    print_info("Запуск DieselMonitor...")

    try:
        from app.main import main
        return main()
    except ImportError as e:
        print_error(f"Ошибка импорта: {e}")
        print_info("Проверьте, что файл app/main.py существует")
        return 1
    except Exception as e:
        print_error(f"Ошибка запуска мониторинга: {e}")
        return 1


def run_web(port: int = 8000, reload: bool = False):
    """Запуск веб-сервера"""
    print_header(f"🌐 ЗАПУСК ВЕБ-СЕРВЕРА (порт {port})")

    try:
        import uvicorn

        # Проверяем наличие web.main
        try:
            from web import main
            print_success("Модуль web.main найден")
        except ImportError as e:
            print_error(f"Модуль web.main не найден: {e}")
            print_info(f"Проверьте, что файл существует: {WEB_DIR / 'main.py'}")
            return 1

        print_info(f"Сервер запускается на http://localhost:{port}")
        print_info("Нажмите Ctrl+C для остановки")
        print("-" * 60)

        uvicorn.run(
            "web.main:app",  # <--- ВАЖНО: web.main, а не app.web.main
            host="0.0.0.0",
            port=port,
            reload=reload,
            log_level="info"
        )
        return 0
    except ImportError as e:
        print_error(f"Ошибка импорта uvicorn: {e}")
        print_info("Установите: pip install uvicorn")
        return 1
    except Exception as e:
        print_error(f"Ошибка запуска веб-сервера: {e}")
        return 1


def run_llm_interactive():
    """Запуск LLM агента в интерактивном режиме"""
    print_header("🤖 ЗАПУСК LLM АГЕНТА (ИНТЕРАКТИВНЫЙ РЕЖИМ)")

    if not LLM_DIR.exists():
        print_error(f"Папка llm_agent не найдена: {LLM_DIR}")
        return 1

    agent_file = LLM_DIR / "simple_agent.py"
    if not agent_file.exists():
        print_error(f"Файл simple_agent.py не найден: {agent_file}")
        return 1

    if not DOCS_DIR.exists():
        print_warning(f"Папка documents не найдена: {DOCS_DIR}")
        print_info("Создайте папку и добавьте документы")
        return 1

    doc_count = len(list(DOCS_DIR.glob("*")))
    if doc_count == 0:
        print_warning(f"Папка documents пуста! Добавьте документы")
        print_info("Поддерживаемые форматы: PDF, DOCX, DOC, TXT, XLSX, PNG, JPG")
        return 1

    print_success(f"Найдено документов: {doc_count}")

    # Создаем chroma_storage если нет
    if not CHROMA_DIR.exists():
        print_info("Создание папки chroma_storage...")
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        print_success(f"Папка создана: {CHROMA_DIR}")

    try:
        from llm_agent.simple_agent import interactive_mode
        interactive_mode()
        return 0
    except ImportError as e:
        print_error(f"Ошибка импорта LLM агента: {e}")
        print_info("Убедитесь, что файл llm_agent/simple_agent.py корректный")
        return 1
    except Exception as e:
        print_error(f"Ошибка запуска LLM агента: {e}")
        return 1


def run_llm_server(port: int = 5002):
    """Запуск LLM сервера (для вебхуков)"""
    print_header(f"🤖 ЗАПУСК LLM СЕРВЕРА (порт {port})")

    try:
        import uvicorn

        try:
            from app.yandex_webhook import webhook_app
            print_success("Модуль вебхука найден")
        except ImportError as e:
            print_error(f"Модуль вебхука не найден: {e}")
            print_info("Проверьте, что файл app/yandex_webhook.py существует")
            return 1

        print_info(f"LLM сервер запускается на http://localhost:{port}")
        print_info(f"Webhook эндпоинт: http://localhost:{port}/webhook")
        print("-" * 60)

        uvicorn.run(
            "app.yandex_webhook:webhook_app",
            host="0.0.0.0",
            port=port,
            log_level="info"
        )
        return 0
    except ImportError as e:
        print_error(f"Ошибка импорта uvicorn: {e}")
        return 1
    except Exception as e:
        print_error(f"Ошибка запуска LLM сервера: {e}")
        return 1


def init_database():
    """Инициализация базы данных"""
    print_header("🗄️ ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ")

    db_path = os.getenv("DB_PATH", "diesel.db")
    db_file = BASE_DIR / db_path

    try:
        from app.db import connect

        print_info(f"Создание базы данных: {db_file}")
        conn = connect(str(db_file))
        conn.close()

        print_success(f"База данных создана: {db_file}")

        import sqlite3
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print_info("Созданные таблицы:")
        for table in tables:
            print(f"  - {table[0]}")
        conn.close()

        return 0
    except Exception as e:
        print_error(f"Ошибка создания базы данных: {e}")
        return 1


def show_status():
    """Показать статус всех компонентов"""
    print_header("📊 СТАТУС СИСТЕМЫ")

    # 1. Проверка БД
    db_file = BASE_DIR / os.getenv("DB_PATH", "diesel.db")
    if db_file.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()

            controllers = cursor.execute("SELECT COUNT(*) FROM controllers").fetchone()[0]

            import time
            today_start = int(time.time() - 86400)
            events = cursor.execute(
                "SELECT COUNT(*) FROM events WHERE ts > ?", (today_start,)
            ).fetchone()[0]

            conn.close()

            print_success(f"База данных: {db_file}")
            print_info(f"  - Контроллеров: {controllers}")
            print_info(f"  - Событий за 24ч: {events}")
        except Exception as e:
            print_warning(f"База данных существует, но ошибка чтения: {e}")
    else:
        print_warning(f"База данных не найдена: {db_file}")

    # 2. Проверка LLM
    if LLM_DIR.exists():
        print_success(f"LLM агент: {LLM_DIR}")

        agent_file = LLM_DIR / "simple_agent.py"
        if agent_file.exists():
            print_success(f"  ✅ simple_agent.py найден")
        else:
            print_warning(f"  ⚠️ simple_agent.py НЕ НАЙДЕН")

        if DOCS_DIR.exists():
            doc_count = len(list(DOCS_DIR.glob("*")))
            if doc_count > 0:
                print_success(f"  ✅ Документы: {doc_count} файлов")
                for f in list(DOCS_DIR.glob("*"))[:5]:
                    print_info(f"     - {f.name}")
                if doc_count > 5:
                    print_info(f"     ... и еще {doc_count - 5} файлов")
            else:
                print_warning("  ⚠️ Документы: папка пуста")
        else:
            print_warning("  ⚠️ Папка documents не найдена")

        if CHROMA_DIR.exists():
            try:
                import chromadb
                from chromadb.config import Settings
                client = chromadb.PersistentClient(path=str(CHROMA_DIR))
                try:
                    collection = client.get_collection("diesel_docs")
                    count = collection.count()
                    print_success(f"  ✅ Векторное хранилище: {count} фрагментов")
                except:
                    print_warning("  ⚠️ Векторное хранилище: коллекция не найдена")
            except Exception as e:
                print_warning(f"  ⚠️ Векторное хранилище: ошибка доступа")
        else:
            print_info("  ℹ️ Папка chroma_storage не создана (будет при первом запуске)")
    else:
        print_error(f"❌ Папка LLM агента НЕ НАЙДЕНА: {LLM_DIR}")

    # 3. Проверка веб-модуля
    if WEB_DIR.exists():
        print_success(f"Веб-модуль: {WEB_DIR}")
        web_main = WEB_DIR / "main.py"
        if web_main.exists():
            print_success(f"  ✅ web/main.py найден")
        else:
            print_warning(f"  ⚠️ web/main.py не найден")
    else:
        print_warning(f"Папка web не найдена: {WEB_DIR}")

    # 4. Проверка конфига
    cfg_file = BASE_DIR / "data" / "cfgmodbus.txt"
    if cfg_file.exists():
        print_success(f"Конфигурация Modbus: {cfg_file}")
    else:
        print_warning(f"Конфигурация Modbus не найдена")

    print_header("✅ СТАТУС ПРОВЕРЕН")


# ============================================================
# ЗАПУСК ВСЕХ КОМПОНЕНТОВ
# ============================================================

class ProcessManager:
    """Управление запущенными процессами"""

    def __init__(self):
        self.processes = []
        self.running = True
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        atexit.register(self.cleanup)

    def signal_handler(self, signum, frame):
        print(f"\n{Colors.YELLOW}⚠️ Получен сигнал остановки...{Colors.RESET}")
        self.running = False
        self.cleanup()

    def cleanup(self):
        print_info("Завершение всех процессов...")
        for p in self.processes:
            try:
                p.terminate()
            except:
                pass
        time.sleep(1)
        for p in self.processes:
            try:
                p.kill()
            except:
                pass

    def run_monitor_thread(self):
        def target():
            from app.main import main
            main()

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def run_web_server(self, port=8000):
        def target():
            import uvicorn
            uvicorn.run(
                "web.main:app",  # <--- ВАЖНО: web.main
                host="0.0.0.0",
                port=port,
                log_level="warning"
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def run_llm_server(self, port=5002):
        def target():
            import uvicorn
            from app.yandex_webhook import webhook_app
            uvicorn.run(webhook_app, host="0.0.0.0", port=port, log_level="warning")

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread


def ensure_chroma_dir():
    """Создает папку chroma_storage если её нет"""
    if not CHROMA_DIR.exists():
        print_info(f"Создание папки chroma_storage: {CHROMA_DIR}")
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        print_success("Папка создана")
        return True
    return False


def run_all(web_port=8000, llm_port=5002):
    """Запуск всех компонентов"""
    print_header("🚀 ЗАПУСК ВСЕХ КОМПОНЕНТОВ")

    # Проверяем наличие LLM
    llm_available = False
    if LLM_DIR.exists():
        agent_file = LLM_DIR / "simple_agent.py"
        if agent_file.exists() and DOCS_DIR.exists():
            doc_count = len(list(DOCS_DIR.glob("*")))
            if doc_count > 0:
                llm_available = True
                print_success(f"LLM агент найден ({doc_count} документов)")
            else:
                print_warning("⚠️ Папка documents пуста")
        else:
            print_warning("⚠️ LLM агент не полностью настроен")
    else:
        print_warning("⚠️ Папка llm_agent не найдена")

    # Создаем chroma_storage если нужно
    ensure_chroma_dir()

    print_info(f"Веб-сервер: http://localhost:{web_port}")
    if llm_available:
        print_info(f"LLM сервер: http://localhost:{llm_port}")
    print_info("Нажмите Ctrl+C для остановки")
    print("-" * 60)

    manager = ProcessManager()

    # Запускаем мониторинг
    print_info("🔄 Запуск мониторинга...")
    monitor_thread = manager.run_monitor_thread()
    manager.processes.append(monitor_thread)
    time.sleep(1)

    # Запускаем веб-сервер
    print_info("🌐 Запуск веб-сервера...")
    web_thread = manager.run_web_server(web_port)
    manager.processes.append(web_thread)
    time.sleep(1)

    # Запускаем LLM сервер (только если всё есть)
    if llm_available:
        print_info("🤖 Запуск LLM сервера...")
        llm_thread = manager.run_llm_server(llm_port)
        manager.processes.append(llm_thread)
        time.sleep(1)
    else:
        print_warning("⚠️ LLM сервер пропущен (не найдены компоненты)")

    print_header("✅ ВСЕ КОМПОНЕНТЫ ЗАПУЩЕНЫ")
    print_info(f"Веб-интерфейс: http://localhost:{web_port}")
    if llm_available:
        print_info(f"LLM webhook: http://localhost:{llm_port}/webhook")
    print("")
    print_info("Ожидание завершения... (Ctrl+C для остановки)")

    try:
        while manager.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print_info("Получен сигнал остановки...")
    finally:
        manager.cleanup()
        print_success("Все компоненты остановлены")


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Универсальный запуск системы мониторинга ДГУ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python run.py                    # Запуск всех компонентов
  python run.py --monitor          # Только мониторинг
  python run.py --web              # Только веб-сервер
  python run.py --web --port 8080  # Веб-сервер на порту 8080
  python run.py --llm              # LLM агент (интерактивный режим)
  python run.py --status           # Проверка статуса
  python run.py --init-db          # Инициализация БД
  python run.py --check            # Проверка окружения
        """
    )

    parser.add_argument("--monitor", action="store_true", help="Запустить только мониторинг")
    parser.add_argument("--web", action="store_true", help="Запустить только веб-сервер")
    parser.add_argument("--llm", action="store_true", help="Запустить LLM агент (интерактивный режим)")
    parser.add_argument("--llm-server", action="store_true", help="Запустить LLM сервер (для вебхуков)")
    parser.add_argument("--all", action="store_true", help="Запустить все компоненты")
    parser.add_argument("--status", action="store_true", help="Показать статус системы")
    parser.add_argument("--init-db", action="store_true", help="Инициализировать базу данных")
    parser.add_argument("--check", action="store_true", help="Проверить окружение")
    parser.add_argument("--port", type=int, default=8000, help="Порт для веб-сервера (по умолчанию 8000)")
    parser.add_argument("--llm-port", type=int, default=5002, help="Порт для LLM сервера (по умолчанию 5002)")
    parser.add_argument("--reload", action="store_true",
                        help="Включить автоперезагрузку для веб-сервера (только разработка)")

    args = parser.parse_args()

    # Если нет аргументов - запускаем все
    if len(sys.argv) == 1:
        check_environment()
        run_all(args.port, args.llm_port)
        return 0

    if args.check:
        check_environment()
        return 0

    if args.status:
        show_status()
        return 0

    if args.init_db:
        return init_database()

    if args.monitor:
        return run_monitor()

    if args.web:
        return run_web(args.port, args.reload)

    if args.llm:
        return run_llm_interactive()

    if args.llm_server:
        return run_llm_server(args.llm_port)

    if args.all:
        check_environment()
        run_all(args.port, args.llm_port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())