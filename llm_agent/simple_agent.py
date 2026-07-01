"""
LLM Агент для работы с документацией ДГУ
Поддерживает: PDF, DOCX, TXT, XLSX, изображения
Языки: русский, английский, чешский, немецкий
"""

import os
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import time
import logging
import re
import hashlib
import shutil

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
from chromadb.config import Settings

# HTTP запросы
import requests
from bs4 import BeautifulSoup

# ============================================================
# 2. НАСТРОЙКИ И ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DOCS_DIR = BASE_DIR / "documents"
CHROMA_DIR = BASE_DIR / "chroma_storage"

DOCS_DIR.mkdir(exist_ok=True)
CHROMA_DIR.mkdir(exist_ok=True)

OCR_LANGUAGES = "rus+eng+ces+deu"
YANDEX_API_KEY_LLM = os.getenv("YANDEX_API_KEY_LLM")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# ============================================================
# 3. ОЧИСТКА ТЕКСТА
# ============================================================

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_llm_output(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines)


# ============================================================
# 4. ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ ДОКУМЕНТОВ
# ============================================================

def extract_text_from_pdf(file_path: str) -> str:
    text_parts = []
    try:
        import fitz
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
        logger.warning(f"PyMuPDF error: {e}")

    try:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page_num, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    text_parts.append(f"--- СТРАНИЦА {page_num + 1} ---\n{text}")
    except Exception as e:
        logger.warning(f"PyPDF2 error: {e}")

    return "\n".join(text_parts)


def extract_text_from_docx(file_path: str) -> str:
    text_parts = []
    try:
        doc = docx.Document(file_path)
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)
        for table_idx, table in enumerate(doc.tables):
            text_parts.append(f"\n[ТАБЛИЦА {table_idx + 1}]")
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    text_parts.append(" | ".join(row_text))
    except Exception as e:
        logger.warning(f"DOCX error: {e}")
    return "\n".join(text_parts)


def extract_text_from_doc(file_path: str) -> str:
    text_parts = []
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
            for encoding in ['cp1251', 'utf-8', 'latin-1', 'cp1250']:
                try:
                    text = content.decode(encoding, errors='ignore')
                    text = re.sub(r'[^\w\s\.,;:!?\-\(\)\n\r%°СВ]', ' ', text)
                    text = re.sub(r'\s+', ' ', text)
                    if text.strip():
                        text_parts.append(text)
                    break
                except:
                    continue
    except Exception as e:
        logger.warning(f"DOC error: {e}")

    if not text_parts:
        try:
            import textract
            text = textract.process(file_path).decode('utf-8')
            if text.strip():
                text_parts.append(text)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"textract error: {e}")

    return "\n".join(text_parts)


def extract_text_from_txt(file_path: str) -> str:
    encodings = ['utf-8', 'cp1251', 'latin-1', 'cp1250', 'koi8-r']
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    return ""


def extract_text_from_excel(file_path: str) -> str:
    try:
        workbook = openpyxl.load_workbook(file_path, data_only=True)
        all_sheets_text = []

        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            sheet_text = [f"\n📊 ЛИСТ: {sheet_name}\n{'-' * 40}"]
            rows_data = []
            for row in sheet.iter_rows(values_only=True):
                row_values = [str(cell) if cell is not None else "" for cell in row]
                if any(v.strip() for v in row_values):
                    rows_data.append(row_values)

            if not rows_data:
                sheet_text.append("(лист пуст)")
                all_sheets_text.append("\n".join(sheet_text))
                continue

            headers = rows_data[0] if rows_data else []
            if not headers or all(not h.strip() for h in headers):
                headers = [f"Колонка_{i+1}" for i in range(len(rows_data[0]))]
                start_row = 0
            else:
                start_row = 1

            if headers:
                sheet_text.append("| " + " | ".join(headers) + " |")
                sheet_text.append("|" + "|".join(["---" for _ in headers]) + "|")
                for row in rows_data[start_row:]:
                    padded_row = row + [""] * (len(headers) - len(row))
                    sheet_text.append("| " + " | ".join(padded_row[:len(headers)]) + " |")

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
        logger.warning(f"Excel error: {e}")
        return ""


def extract_text_from_image(file_path: str) -> str:
    try:
        image = Image.open(file_path)
        if image.width < 1000:
            scale = 2000 / image.width
            new_size = (int(image.width * scale), int(image.height * scale))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        text = pytesseract.image_to_string(image, lang=OCR_LANGUAGES)
        return text.strip() if text.strip() else ""
    except Exception as e:
        logger.warning(f"Image OCR error: {e}")
        return ""

def generate_chunk_id(
    filename,
    chunk_number,
    chunk_text
):

    data = (
        filename
        + str(chunk_number)
        + chunk_text[:500]
    )

    return hashlib.md5(
        data.encode("utf-8")
    ).hexdigest()

def load_all_documents() -> List[Dict]:
    documents = []
    logger.info(f"Сканируем папку: {DOCS_DIR}")

    supported_extensions = ['.pdf', '.docx', '.doc', '.txt', '.xlsx', '.xlsm', '.png', '.jpg', '.jpeg', '.bmp', '.tiff']

    for file_path in DOCS_DIR.iterdir():
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext not in supported_extensions:
            logger.warning(f"Неподдерживаемый формат: {file_path.name}")
            continue

        logger.info(f"Обработка: {file_path.name}")

        if ext == '.pdf':
            text = extract_text_from_pdf(str(file_path))
        elif ext == '.docx':
            text = extract_text_from_docx(str(file_path))
        elif ext == '.doc':
            text = extract_text_from_doc(str(file_path))
        elif ext == '.txt':
            text = extract_text_from_txt(str(file_path))
        elif ext in ['.xlsx', '.xlsm']:
            text = extract_text_from_excel(str(file_path))
        elif ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
            text = extract_text_from_image(str(file_path))
        else:
            continue

        text = sanitize_text(text)

        if text and text.strip():
            chunk_size = 2000
            overlap = 300
            chunks = []
            for i in range(0, len(text), chunk_size - overlap):
                chunk = text[i:i + chunk_size]
                if chunk.strip():
                    chunks.append(chunk)

            for i, chunk in enumerate(chunks):
                documents.append({
                    "id": generate_chunk_id(file_path.name,i,chunk),
                    "text": chunk,
                    "metadata": {
                        "source": file_path.name,
                        "type": "excel" if ext in ['.xlsx', '.xlsm'] else "document",
                        "chunk": i,
                        "total_chunks": len(chunks),
                        "processed_at": datetime.now().isoformat()
                    }
                })
            logger.info(f"  ✅ Загружено: {len(chunks)} фрагментов")
        else:
            logger.warning(f"  ⚠️ Текст не извлечён")

    return documents


# ============================================================
# 5. ВЕКТОРНОЕ ХРАНИЛИЩЕ (СОХРАНЕНИЕ МЕЖДУ ЗАПУСКАМИ)
# ============================================================

class VectorStore:
    COLLECTION_NAME = "diesel_docs"

    def __init__(self):
        logger.info("Инициализация VectorStore")

        self.embedding_model = SentenceTransformer(
            "intfloat/multilingual-e5-small"
        )

        self.embedding_model.max_seq_length = 512

        self.client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True
            )
        )

        self.collection = self.client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )

        logger.info(
            f"Коллекция открыта. Чанков: {self.collection.count()}"
        )

        self._validate_collection()

    def _validate_collection(self):
        """
        Проверка индекса после старта.
        Если индекс битый — пересоздаем.
        """

        count = self.collection.count()

        if count == 0:
            return

        try:
            dummy = [0.0] * 384

            self.collection.query(
                query_embeddings=[dummy],
                n_results=1
            )

            logger.info("Индекс успешно проверен")

        except Exception as e:
            logger.error(
                f"Коллекция повреждена: {e}"
            )

            raise RuntimeError(
                "Поврежден индекс Chroma"
            )

    def embed_documents(self, texts):
        prefixed = [
            f"passage: {t}"
            for t in texts
        ]

        return self.embedding_model.encode(
            prefixed,
            normalize_embeddings=True,
            convert_to_numpy=True
        ).tolist()

    def embed_query(self, query):
        return self.embedding_model.encode(
            [f"query: {query}"],
            normalize_embeddings=True,
            convert_to_numpy=True
        ).tolist()

    def index_documents(self, documents):

        batch_size = 200

        for start in range(
                0,
                len(documents),
                batch_size
        ):
            batch = documents[
                    start:start + batch_size
                    ]

            ids = [d["id"] for d in batch]
            texts = [d["text"] for d in batch]
            metas = [d["metadata"] for d in batch]

            embeddings = self.embed_documents(texts)

            try:
                self.collection.upsert(
                    ids=ids,
                    documents=texts,
                    metadatas=metas,
                    embeddings=embeddings
                )
            except Exception as e:
                logger.error(f"Ошибка индексации: {e}")

            logger.info(
                f"Добавлено {len(ids)} чанков"
            )

    def search(self, query, top_k=5):

        query_embedding = self.embed_query(query)

        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=top_k
        )

        output = []

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for i in range(len(docs)):
            output.append({
                "text": docs[i],
                "metadata": metas[i],
                "distance": dists[i]
            })

        return output

    def get_stats(self):
        return {
            "collection_name": self.COLLECTION_NAME,
            "total_chunks": self.collection.count()
        }


# ============================================================
# 6. YANDEX GPT
# ============================================================

class YandexGPT:
    def __init__(self):
        self.api_key = YANDEX_API_KEY_LLM
        self.folder_id = YANDEX_FOLDER_ID
        self.model = "yandexgpt-lite"

        self.system_prompt = """Ты технический эксперт по дизельным генераторным установкам (ДГУ).

ПРАВИЛА ОТВЕТОВ:
1. Отвечай ТОЛЬКО на русском языке.
2. Используй ТОЛЬКО информацию из предоставленной документации.
3. Если в документации нет ответа - скажи "В документации нет информации об этом".
4. Если вопрос не по теме ДГУ - вежливо откажись отвечать.
5. Будь конкретным и профессиональным.
6. Если есть таблица с нормами - используй её данные.
7. При опасных ситуациях - предупреди пользователя.

ФОРМАТИРОВАНИЕ ОТВЕТОВ (ОБЯЗАТЕЛЬНО):
- НЕ ИСПОЛЬЗУЙ HTML-теги (например, <br>, <p>, <div>).
- НЕ ИСПОЛЬЗУЙ Markdown (**, *, #).
- Для переноса строк используй ТОЛЬКО символы перевода строки (два \n\n для абзацев).
- Для списков используй дефис или звёздочку в начале строки.
- Не добавляй лишних пробелов в начале строк.

Пример правильного ответа:
Температура охлаждающей жидкости 93°C - это выше нормы.

Возможные причины:
- Неисправность термостата
- Засорение радиатора
- Низкий уровень антифриза

Рекомендуется проверить уровень антифриза и работу термостата."""

    def ask(self, question: str, context: str) -> str:
        prompt = f"""
ДОКУМЕНТАЦИЯ:
{context}

ВОПРОС ПОЛЬЗОВАТЕЛЯ:
{question}

ОТВЕТ (на русском языке, без HTML-тегов, без Markdown):
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
            answer = clean_llm_output(answer)
            return answer
        except Exception as e:
            logger.error(f"Ошибка Yandex GPT: {e}")
            return f"Извините, произошла ошибка при обращении к модели: {e}"

    def check_connection(self) -> bool:
        try:
            self.ask("Тест", "Тестовый контекст")
            return True
        except:
            return False


# ============================================================
# 7. ОСНОВНОЙ КЛАСС АГЕНТА (ТОЛЬКО LLM)
# ============================================================

class DieselLLMAgent:
    def __init__(self, auto_index: bool = True):
        logger.info("="*60)
        logger.info("🚀 ЗАПУСК LLM АГЕНТА ДЛЯ ДГУ")
        logger.info("="*60)

        self.vector_store = None
        self.llm = None
        self.is_initialized = False

        try:
            self.vector_store = VectorStore()
            self.llm = YandexGPT()
            self.is_initialized = True
        except Exception as e:
            logger.error(f"Ошибка инициализации: {e}")
            logger.warning("⚠️ Агент будет работать в ограниченном режиме (без поиска)")

        self.document_stats = {"total_docs": 0, "sources": []}

        if auto_index and self.is_initialized:
            self._initialize_documents()

        logger.info("✅ АГЕНТ ГОТОВ К РАБОТЕ!")
        logger.info("="*60)

    def _initialize_documents(self):
        """Загрузка или использование существующей базы"""
        if not self.is_initialized:
            return

        try:
            count = self.vector_store.collection.count()
            if count > 0:
                logger.info(f"📚 Использую существующую базу ({count} фрагментов)")
                self._update_stats()
                return
            else:
                logger.info("🔄 Коллекция существует, но пуста. Загружаю документы...")
                documents = load_all_documents()
                if documents:
                    self.vector_store.index_documents(documents)
                    self._update_stats()
                else:
                    logger.warning("⚠️ НЕТ ДОКУМЕНТОВ ДЛЯ ЗАГРУЗКИ!")
                    logger.info(f"📁 Добавьте файлы в папку: {DOCS_DIR}")
        except Exception as e:
            logger.error(f"Ошибка доступа к коллекции: {e}")
            logger.info("💡 Попробуйте выполнить восстановление через /refresh")

    def _update_stats(self):
        try:
            results = self.vector_store.collection.get(include=["metadatas"])
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
            logger.warning(f"Ошибка статистики: {e}")

    def ask(self, question: str, context_override: str = None) -> Dict:
        if not self.is_initialized:
            return {
                "answer": "Извините, агент не инициализирован. Проверьте настройки.",
                "sources": [],
                "question": question
            }

        logger.info(f"Вопрос: {question[:100]}..." if len(question) > 100 else f"Вопрос: {question}")

        try:
            relevant_docs = self.vector_store.search(question, top_k=5)
        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            return {
                "answer": f"Ошибка поиска в документации: {e}",
                "sources": [],
                "question": question
            }

        if not relevant_docs:
            return {
                "answer": "В загруженной документации не найдено информации по вашему вопросу.\n\n💡 Убедитесь, что документы загружены в папку llm_agent/documents/",
                "sources": [],
                "question": question
            }

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

        try:
            answer = self.llm.ask(question, context)
        except Exception as e:
            logger.error(f"Ошибка LLM: {e}")
            return {
                "answer": f"Ошибка при обращении к модели: {e}",
                "sources": sources,
                "question": question
            }

        return {
            "answer": answer,
            "sources": sources,
            "question": question
        }

    def ask_with_status(self, question: str, monitoring_data: Dict) -> Dict:
        status_keywords = [
            'температура', 'давление', 'обороты', 'напряжение',
            'ток', 'частота', 'уровень', 'режим', 'состояние',
            'параметр', 'показатель', 'норма', 'отклонение',
            'авария', 'ошибка', 'неисправность', 'проблема'
        ]
        question_lower = question.lower()
        use_status = any(kw in question_lower for kw in status_keywords)

        if use_status and monitoring_data:
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
        else:
            return self.ask(question)

    def get_stats(self) -> dict:
        if not self.is_initialized:
            return {"status": "not_initialized", "error": "Агент не инициализирован"}
        try:
            return {
                "status": "ready",
                "vector_store": self.vector_store.get_stats(),
                "documents": self.document_stats,
                "llm_model": "yandexgpt-lite"
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def refresh_documents(self) -> dict:
        """Принудительное обновление (удаляет старую коллекцию и пересоздаёт)"""
        if not self.is_initialized:
            return {"status": "error", "message": "Агент не инициализирован"}

        logger.info("🔄 Принудительное обновление документов...")

        try:
            # Удаляем старую коллекцию
            self.vector_store.client.delete_collection("diesel_docs")
            shutil.rmtree(CHROMA_DIR, ignore_errors=True)
            CHROMA_DIR.mkdir(exist_ok=True)
            # Создаём новую
            self.vector_store = VectorStore()
            # Индексируем документы
            documents = load_all_documents()
            if documents:
                self.vector_store.index_documents(documents)
                self._update_stats()
                return {"status": "success", "documents_count": len(documents)}
            else:
                return {"status": "error", "message": "Нет документов для индексации"}
        except Exception as e:
            logger.error(f"Ошибка обновления: {e}")
            return {"status": "error", "message": str(e)}

    def get_available_sources(self) -> List[str]:
        return self.document_stats.get("sources", [])