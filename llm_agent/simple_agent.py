"""
LLM Агент для работы с документацией ДГУ
Поддерживает: PDF, DOCX, TXT, XLSX, изображения
Языки: русский, английский, чешский, немецкий
"""

import os
import sys
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

# Загрузка переменных окружения
from dotenv import load_dotenv
load_dotenv()

# ============================================================
# 1. ИМПОРТ БИБЛИОТЕК
# ============================================================

# Работа с документами
import PyPDF2
import docx
import openpyxl
from PIL import Image
import pytesseract

# Эмбеддинги и поиск
from sentence_transformers import SentenceTransformer
import chromadb

# HTTP запросы
import requests

# ============================================================
# 2. НАСТРОЙКИ
# ============================================================

BASE_DIR = Path(__file__).parent
DOCS_DIR = BASE_DIR / "documents"
CHROMA_DIR = BASE_DIR / "chroma_storage"

# Создаём папки
DOCS_DIR.mkdir(exist_ok=True)
CHROMA_DIR.mkdir(exist_ok=True)

# Языки для OCR (русский, английский, чешский, немецкий)
OCR_LANGUAGES = "rus+eng+ces+deu"

# Путь к Tesseract (для Windows - раскомментировать и указать свой путь)
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# API Yandex GPT
YANDEX_API_KEY_LLM = os.getenv("YANDEX_API_KEY_LLM")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# ============================================================
# 3. ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ ДОКУМЕНТОВ
# ============================================================

def extract_text_from_pdf(file_path: str) -> str:
    """Извлечение текста из PDF (с поддержкой скан-копий)"""
    text_parts = []

    # Пробуем PyMuPDF (лучше справляется)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        for page_num, page in enumerate(doc):
            text = page.get_text()
            if text and text.strip():
                text_parts.append(f"--- СТРАНИЦА {page_num + 1} ---\n{text}")
        doc.close()

        if text_parts:
            return "\n".join(text_parts)
    except ImportError:
        pass
    except Exception as e:
        print(f"  PyMuPDF error: {e}")

    # Fallback на PyPDF2
    try:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page_num, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    text_parts.append(f"--- СТРАНИЦА {page_num + 1} ---\n{text}")
    except Exception as e:
        print(f"  PyPDF2 error: {e}")

    return "\n".join(text_parts)


def extract_text_from_docx(file_path: str) -> str:
    """Извлечение текста из DOCX, включая таблицы"""
    text_parts = []
    try:
        doc = docx.Document(file_path)

        # Обычные параграфы
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)

        # Таблицы
        for table_idx, table in enumerate(doc.tables):
            text_parts.append(f"\n[ТАБЛИЦА {table_idx + 1}]")
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text:
                        row_text.append(cell_text)
                if row_text:
                    text_parts.append(" | ".join(row_text))

    except Exception as e:
        print(f"  DOCX error: {e}")
    return "\n".join(text_parts)


# ДОБАВЬТЕ ЭТУ НОВУЮ ФУНКЦИЮ
def extract_text_from_doc(file_path: str) -> str:
    """Извлечение текста из старого .doc файла"""
    text_parts = []
    try:
        # Пробуем прочитать как бинарный файл и извлечь текст
        with open(file_path, 'rb') as f:
            content = f.read()
            # Пробуем разные кодировки
            for encoding in ['cp1251', 'utf-8', 'latin-1', 'cp1250']:
                try:
                    text = content.decode(encoding, errors='ignore')
                    # Очищаем от нечитаемых символов
                    import re
                    text = re.sub(r'[^\w\s\.,;:!?\-\(\)\n\r%°СВ]', ' ', text)
                    text = re.sub(r'\s+', ' ', text)
                    if text.strip():
                        text_parts.append(text)
                    break
                except:
                    continue
    except Exception as e:
        print(f"  DOC error: {e}")

    if not text_parts:
        # Если не получилось, пробуем через textract (если установлен)
        try:
            import textract
            text = textract.process(file_path).decode('utf-8')
            if text.strip():
                text_parts.append(text)
        except ImportError:
            print("  ⚠️ Для чтения .doc файлов установите: pip install textract")
        except Exception as e:
            print(f"  textract error: {e}")

    return "\n".join(text_parts)

def extract_text_from_txt(file_path: str) -> str:
    """Извлечение текста из TXT с автоопределением кодировки"""
    encodings = ['utf-8', 'cp1251', 'latin-1', 'cp1250', 'koi8-r']
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    return ""


def extract_text_from_excel(file_path: str) -> str:
    """Извлечение данных из Excel (XLSX) с сохранением структуры"""
    try:
        workbook = openpyxl.load_workbook(file_path, data_only=True)
        all_sheets_text = []

        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            sheet_text = [f"\n📊 ЛИСТ: {sheet_name}\n{'-' * 40}"]

            # Собираем все строки с данными
            rows_data = []
            for row in sheet.iter_rows(values_only=True):
                row_values = [str(cell) if cell is not None else "" for cell in row]
                if any(v.strip() for v in row_values):
                    rows_data.append(row_values)

            if not rows_data:
                sheet_text.append("(лист пуст)")
                all_sheets_text.append("\n".join(sheet_text))
                continue

            # Определяем заголовки (первая строка)
            headers = rows_data[0] if rows_data else []

            # Если заголовков нет - создаём
            if not headers or all(not h.strip() for h in headers):
                headers = [f"Колонка_{i+1}" for i in range(len(rows_data[0]))]
                start_row = 0
            else:
                start_row = 1

            # Формируем Markdown-таблицу
            if headers:
                sheet_text.append("| " + " | ".join(headers) + " |")
                sheet_text.append("|" + "|".join(["---" for _ in headers]) + "|")

                for row in rows_data[start_row:]:
                    # Выравниваем строку по количеству колонок
                    padded_row = row + [""] * (len(headers) - len(row))
                    sheet_text.append("| " + " | ".join(padded_row[:len(headers)]) + " |")

            # Дополнительно: формат ключ-значение для лучшего понимания
            sheet_text.append("\n📋 ДАННЫЕ (ключ-значение):")
            for row_idx, row in enumerate(rows_data[start_row:], 1):
                row_parts = []
                for col_idx, value in enumerate(row):
                    if col_idx < len(headers) and headers[col_idx] and value:
                        row_parts.append(f"{headers[col_idx]}: {value}")
                if row_parts:
                    sheet_text.append(f"Строка {row_idx}: " + ", ".join(row_parts))

            all_sheets_text.append("\n".join(sheet_text))

        workbook.close()
        return "\n\n".join(all_sheets_text)

    except Exception as e:
        print(f"  Excel error: {e}")
        return ""


def extract_text_from_image(file_path: str) -> str:
    """Извлечение текста из изображения через OCR"""
    try:
        image = Image.open(file_path)
        # Увеличиваем размер для лучшего распознавания
        if image.width < 1000:
            scale = 2000 / image.width
            new_size = (int(image.width * scale), int(image.height * scale))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        text = pytesseract.image_to_string(image, lang=OCR_LANGUAGES)
        return text.strip() if text.strip() else ""
    except Exception as e:
        print(f"  Image OCR error: {e}")
        return ""


def load_all_documents() -> List[Dict]:
    """Загрузка всех документов из папки documents/"""
    documents = []

    print(f"\n📁 Сканируем папку: {DOCS_DIR}")

    # Поддерживаемые форматы
    supported_extensions = ['.pdf', '.docx', '.doc', '.txt', '.xlsx', '.xlsm', '.png', '.jpg', '.jpeg', '.bmp', '.tiff']
    for file_path in DOCS_DIR.iterdir():
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext not in supported_extensions:
            print(f"\n⚠️ Неподдерживаемый формат: {file_path.name} ({ext})")
            continue

        print(f"\n📄 Обработка: {file_path.name}")

        # Выбираем функцию извлечения в зависимости от расширения
        if ext == '.pdf':
            text = extract_text_from_pdf(str(file_path))
        elif ext == '.docx':
            text = extract_text_from_docx(str(file_path))
        elif ext == '.doc':
            text = extract_text_from_doc(str(file_path))  # <- И ЭТУ
        elif ext == '.txt':
            text = extract_text_from_txt(str(file_path))
        elif ext in ['.xlsx', '.xlsm']:
            text = extract_text_from_excel(str(file_path))
        elif ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
            text = extract_text_from_image(str(file_path))
        else:
            continue

        if text and text.strip():
            # Разбиваем на чанки (для таблиц увеличил размер)
            chunk_size = 2000
            overlap = 300
            chunks = []
            for i in range(0, len(text), chunk_size - overlap):
                chunk = text[i:i + chunk_size]
                if chunk.strip():
                    chunks.append(chunk)

            for i, chunk in enumerate(chunks):
                documents.append({
                    "id": f"{file_path.name}_{i}",
                    "text": chunk,
                    "metadata": {
                        "source": file_path.name,
                        "type": "excel" if ext in ['.xlsx', '.xlsm'] else "document",
                        "chunk": i,
                        "total_chunks": len(chunks),
                        "processed_at": datetime.now().isoformat()
                    }
                })
            print(f"  ✅ Загружено: {len(chunks)} фрагментов")
        else:
            print(f"  ⚠️ Текст не извлечён (файл пуст или нечитаем)")

    return documents


# ============================================================
# 4. ВЕКТОРНОЕ ХРАНИЛИЩЕ (ЭМБЕДДИНГИ)
# ============================================================

class VectorStore:
    """Хранилище эмбеддингов на базе ChromaDB"""

    def __init__(self):
        print("\n🔄 Инициализация векторного хранилища...")

        # Загружаем модель эмбеддингов
        print("  - Загрузка модели эмбеддингов (первый раз может занять минуту)...")
        self.embedding_model = SentenceTransformer('intfloat/multilingual-e5-small')

        # Создаём клиент ChromaDB
        print("  - Подключение к ChromaDB...")
        self.client = chromadb.PersistentClient(path=str(CHROMA_DIR))

        # Получаем или создаём коллекцию
        self.collection = self.client.get_or_create_collection(
            name="diesel_docs"
        )

        print(f"  ✅ Готово. В коллекции: {self.collection.count()} фрагментов")

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Получение эмбеддингов для текстов"""
        return self.embedding_model.encode(texts, convert_to_numpy=True).tolist()

    def index_documents(self, documents: List[Dict]):
        """Индексация документов в ChromaDB с пакетной обработкой"""
        if not documents:
            print("⚠️ Нет документов для индексации")
            return

        print(f"\n🔄 Индексация {len(documents)} фрагментов...")

        # Разбиваем на пакеты по 500 (безопасный размер)
        BATCH_SIZE = 500
        total_batches = (len(documents) + BATCH_SIZE - 1) // BATCH_SIZE

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch_end = min(batch_start + BATCH_SIZE, len(documents))
            batch = documents[batch_start:batch_end]

            print(f"  - Пакет {batch_num + 1}/{total_batches} ({len(batch)} фрагментов)")

            # Подготавливаем данные
            ids = [doc["id"] for doc in batch]
            texts = [doc["text"] for doc in batch]
            metadatas = [doc["metadata"] for doc in batch]

            # Получаем эмбеддинги
            print("    - Вычисление эмбеддингов...")
            embeddings = self._get_embeddings(texts)

            # Сохраняем в ChromaDB
            print("    - Сохранение в базу...")
            self.collection.add(
                ids=ids,
                documents=texts,
                metadatas=metadatas,
                embeddings=embeddings
            )

        print(f"  ✅ Индексация завершена. Всего: {self.collection.count()}")

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """Поиск релевантных фрагментов"""
        # Получаем эмбеддинг запроса
        query_embedding = self._get_embeddings([query])[0]

        # Поиск в ChromaDB
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k
        )

        # Форматируем результаты
        documents = []
        if results['documents'] and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                documents.append({
                    "text": doc,
                    "metadata": results['metadatas'][0][i] if results['metadatas'] else {},
                    "distance": results['distances'][0][i] if results['distances'] else None
                })

        return documents

    def get_stats(self) -> dict:
        """Получить статистику хранилища"""
        return {
            "total_chunks": self.collection.count(),
            "collection_name": "diesel_docs"
        }


# ============================================================
# 5. YANDEX GPT (ГЕНЕРАЦИЯ ОТВЕТОВ)
# ============================================================

class YandexGPT:
    """Клиент для Yandex GPT API"""

    def __init__(self):
        self.api_key = YANDEX_API_KEY_LLM
        self.folder_id = YANDEX_FOLDER_ID
        self.model = "yandexgpt-lite"

        self.system_prompt = """Ты технический эксперт по дизельным генераторным установкам (ДГУ).

        ПРАВИЛА:
        1. Отвечай ТОЛЬКО на русском языке.
        2. Используй ТОЛЬКО информацию из предоставленной документации.
        3. Если в документации нет ответа - скажи "В документации нет информации об этом".
        4. Если вопрос не по теме ДГУ - вежливо откажись отвечать.
        5. Будь конкретным и профессиональным.
        6. Если есть таблица с нормами - используй её данные.
        7. При опасных ситуациях - предупреди пользователя.

        ВАЖНОЕ ПРАВИЛО ФОРМАТИРОВАНИЯ:
        - НЕ ИСПОЛЬЗУЙ HTML-теги (например, <br>, <p>, <div>).
        - Для переноса строк используй только символ перевода строки (два подряд \n\n для абзацев).
        - Для списков используй дефис или звёздочку в начале строки.
        - Не добавляй лишних пробелов в начале строк.

        Пример правильного ответа:
        **Температура ОЖ 93°C** – это выше нормы.
        Возможные причины:
        - Неисправность термостата
        - Засорение радиатора
        Рекомендуется проверить уровень антифриза."""

    def ask(self, question: str, context: str) -> str:
        """Задать вопрос с контекстом из документов"""

        prompt = f"""
ДОКУМЕНТАЦИЯ:
{context}

ВОПРОС ПОЛЬЗОВАТЕЛЯ:
{question}

ОТВЕТ (на русском языке):
"""

        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "modelUri": f"gpt://{self.folder_id}/{self.model}",
            "completionOptions": {
                "temperature": 0.1,
                "maxTokens": 2000
            },
            "messages": [
                {"role": "system", "text": self.system_prompt},
                {"role": "user", "text": prompt}
            ]
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            response.raise_for_status()
            result = response.json()
            answer = result["result"]["alternatives"][0]["message"]["text"]
            # Убираем HTML-теги переноса строки (модель иногда их генерирует)
            answer = answer.replace('<br>', '\n').replace('<br/>', '\n')
            return answer
        except Exception as e:
            print(f"❌ Ошибка Yandex GPT: {e}")
            return f"Извините, произошла ошибка при обращении к модели: {e}"

    def check_connection(self) -> bool:
        """Проверка подключения к API"""
        try:
            self.ask("Тест", "Тестовый контекст")
            return True
        except:
            return False


# ============================================================
# 6. ОСНОВНОЙ КЛАСС АГЕНТА
# ============================================================

class DieselLLMAgent:
    """LLM агент для работы с документацией ДГУ"""

    def __init__(self, auto_index: bool = True):
        print("\n" + "="*60)
        print("🚀 ЗАПУСК LLM АГЕНТА ДЛЯ ДГУ")
        print("="*60)

        # Инициализация компонентов
        self.vector_store = VectorStore()
        self.llm = YandexGPT()

        # Загружаем документы (только если нужно и если есть новые)
        self.document_stats = {"total_docs": 0, "sources": []}

        if auto_index:
            self._initialize_documents()

        print("\n✅ АГЕНТ ГОТОВ К РАБОТЕ!")
        print("="*60)

    def _initialize_documents(self):
        """Загрузка и индексация документов (только если нет в базе)"""

        # Проверяем, есть ли уже документы
        if self.vector_store.collection.count() > 0:
            print(f"\n📚 Документы уже загружены ({self.vector_store.collection.count()} фрагментов)")
            self._update_stats()
            return

        # Загружаем документы из папки
        documents = load_all_documents()

        if documents:
            self.vector_store.index_documents(documents)
            self._update_stats()
        else:
            print("\n⚠️ НЕТ ДОКУМЕНТОВ ДЛЯ ЗАГРУЗКИ!")
            print(f"📁 Добавьте файлы (PDF, DOCX, XLSX, TXT, изображения) в папку: {DOCS_DIR}")

    def _update_stats(self):
        """Обновление статистики документов"""
        try:
            # Получаем список уникальных источников
            results = self.vector_store.collection.get()
            if results and results['metadatas']:
                sources = set()
                for meta in results['metadatas']:
                    if meta and 'source' in meta:
                        sources.add(meta['source'])
                self.document_stats = {
                    "total_docs": len(sources),
                    "sources": list(sources),
                    "total_chunks": len(results['ids'])
                }
        except Exception as e:
            print(f"⚠️ Ошибка статистики: {e}")

    def ask(self, question: str, context_override: str = None) -> Dict:
        """Задать вопрос агенту"""

        print(f"\n❓ Вопрос: {question[:100]}..." if len(question) > 100 else f"\n❓ Вопрос: {question}")

        # Поиск релевантных фрагментов
        relevant_docs = self.vector_store.search(question, top_k=5)

        if not relevant_docs:
            return {
                "answer": "В загруженной документации не найдено информации по вашему вопросу.\n\n💡 Совет: Убедитесь, что документы загружены в папку llm_agent/documents/",
                "sources": [],
                "question": question
            }

        # Формируем контекст
        context_parts = []
        sources = []

        for doc in relevant_docs:
            context_parts.append(doc["text"])
            sources.append({
                "source": doc["metadata"].get("source", "unknown"),
                "text": doc["text"][:300] + "..." if len(doc["text"]) > 300 else doc["text"]
            })

        context = "\n\n---\n\n".join(context_parts)

        if context_override:
            context = context_override

        # Получаем ответ от LLM
        answer = self.llm.ask(question, context)

        return {
            "answer": answer,
            "sources": sources,
            "question": question
        }

    def ask_with_status(self, question: str, monitoring_data: Dict) -> Dict:
        """Задать вопрос с учётом текущего состояния установки"""

        # Формируем контекст из данных мониторинга
        status_text = f"""
ТЕКУЩЕЕ СОСТОЯНИЕ УСТАНОВКИ:
- Обороты: {monitoring_data.get('rpm', 'нет данных')} об/мин
- Температура ОЖ: {monitoring_data.get('temp', 'нет данных')}°C
- Давление масла: {monitoring_data.get('oil', 'нет данных')} Бар
- Напряжение АКБ: {monitoring_data.get('battery', 'нет данных')} В
- Уровень топлива: {monitoring_data.get('fuel', 'нет данных')} %
- Режим работы: {monitoring_data.get('mode', 'НОРМАЛЬНЫЙ')}
- Установка: {monitoring_data.get('unit_name', 'ДГУ')}

Задача: Проанализируй эти параметры и ответь на вопрос пользователя, используя документацию.

Вопрос: {question}
"""
        return self.ask(status_text)

    def get_stats(self) -> dict:
        """Получить статистику агента"""
        return {
            "status": "ready",
            "vector_store": self.vector_store.get_stats(),
            "documents": self.document_stats,
            "llm_model": "yandexgpt-lite"
        }

    def refresh_documents(self) -> dict:
        """Принудительное обновление документов (очистка и переиндексация)"""
        print("\n🔄 Принудительное обновление документов...")

        # Очищаем коллекцию
        self.vector_store.collection.delete(where={})

        # Переиндексируем
        documents = load_all_documents()
        if documents:
            self.vector_store.index_documents(documents)
            self._update_stats()
            return {"status": "success", "documents_count": len(documents)}
        else:
            return {"status": "error", "message": "Нет документов для индексации"}

    def get_available_sources(self) -> List[str]:
        """Получить список доступных источников документов"""
        return self.document_stats.get("sources", [])


# ============================================================
# 7. ЗАПУСК В ИНТЕРАКТИВНОМ РЕЖИМЕ (ДЛЯ ТЕСТИРОВАНИЯ)
# ============================================================

def interactive_mode():
    """Интерактивный режим для тестирования в консоли"""
    agent = DieselLLMAgent()

    print("\n" + "="*60)
    print("💬 ИНТЕРАКТИВНЫЙ РЕЖИМ")
    print("Команды:")
    print("  /status   - показать статистику")
    print("  /sources  - показать загруженные документы")
    print("  /refresh  - переиндексировать документы")
    print("  /выход    - завершить работу")
    print("="*60)

    # Тестовые данные для статуса
    test_monitoring = {
        "rpm": 1500,
        "temp": 83,
        "oil": 1.1,
        "battery": 27.3,
        "fuel": 72,
        "mode": "НОРМАЛЬНЫЙ",
        "unit_name": "Тестовая ДГУ"
    }

    while True:
        user_input = input("\n❓ Вы: ").strip()

        if not user_input:
            continue

        # Обработка команд
        if user_input.lower() in ['выход', 'exit', 'quit', '/выход']:
            print("👋 До свидания!")
            break

        if user_input.lower() == '/status':
            stats = agent.get_stats()
            print(f"\n📊 СТАТИСТИКА:")
            print(f"   Статус: {stats['status']}")
            print(f"   Фрагментов в базе: {stats['vector_store']['total_chunks']}")
            print(f"   Документов: {stats['documents']['total_docs']}")
            if stats['documents']['sources']:
                print(f"   Источники:")
                for src in stats['documents']['sources'][:5]:
                    print(f"     - {src}")
            continue

        if user_input.lower() == '/sources':
            sources = agent.get_available_sources()
            if sources:
                print(f"\n📚 ЗАГРУЖЕННЫЕ ДОКУМЕНТЫ:")
                for src in sources:
                    print(f"   - {src}")
            else:
                print("\n📭 Нет загруженных документов")
            continue

        if user_input.lower() == '/refresh':
            result = agent.refresh_documents()
            if result['status'] == 'success':
                print(f"\n✅ Документы обновлены! Загружено фрагментов: {result['documents_count']}")
            else:
                print(f"\n❌ Ошибка: {result.get('message', 'неизвестная ошибка')}")
            continue

        # Анализ параметров
        if user_input.lower().startswith('/анализ') or 'параметр' in user_input.lower():
            result = agent.ask_with_status(user_input, test_monitoring)
        else:
            result = agent.ask(user_input)

        print(f"\n🤖 Агент: {result['answer']}")

        if result.get('sources'):
            print(f"\n📚 Источники:")
            for src in result['sources'][:3]:
                print(f"   - {src['source']}")


if __name__ == "__main__":
    interactive_mode()