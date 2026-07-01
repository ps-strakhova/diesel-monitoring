# ⚡ Система мониторинга дизельных электростанций (ДГУ) с AI ассистентом

Интеллектуальная система для мониторинга, диагностики и оповещения о состоянии дизельных генераторных установок с встроенным AI ассистентом на базе Yandex GPT.

## Возможности

- **Мониторинг в реальном времени** - чтение данных по Modbus TCP
- **Аварийное оповещение** - Telegram, Яндекс Мессенджер
- **Веб-интерфейс** - графики, статус, история событий
- **AI ассистент** - отвечает на вопросы по документации ДГУ
- **Экспорт отчетов** - Excel, CSV, DOCX
- **База знаний** - накопление и поиск информации

## Требования

- Python 3.9+
- SQLite3
- Modbus TCP доступ к контроллеру ДГУ
- Telegram Bot Token (для уведомлений)
- Yandex GPT API Key (для AI ассистента)

## Быстрый старт

### 1. Клонирование репозитория

```bash
git clone <https://github.com/ps-strakhova/diesel-monitoring>
cd diesel-monitoring

diesel/
├── app/
│    ├── core/
│    │    ├──__init__.py
│    │    ├── constants.py
│    ├── services/
│    │    ├── __init__.py
│    │    ├── alarm_service.py
│    │    ├── bit_processor.py
│    │    ├── status_service.py
│    │    ├── stop_service.py
│    ├── __init__.py
│    ├── canonical_keys.py
│    ├── cfg_parser.py
│    ├── config.py
│    ├── db.py
│    ├── detector.py
│    ├── kb_seed.py
│    ├── main.py
│    ├── modbus_client.py
│    ├── notifier_telegram.py
│    ├── notifier_yandex.py
│    ├── status_renderer.py
│    ├── storage.py
│    ├── telegram_commands.py
│    ├── yandex_commands.py
│    ├── yandex_webhook.py
├── data
│    ├── cfgmodbus.txt
├── llm_agent/                    
│   ├── simple_agent.py       
│   ├── __init__.py   
│   ├── documents/               
│   │   ├── manual.pdf
│   │   ├── instructions.docx
│   │   └── ...
│   └── chroma_storage/      
├── web/
│    ├── static
│    │    ├── css
│    │    ├── images
│    │    ├── js
│    │    │    ├── main.js
│    ├── templates
│    │    ├── index.html
│   ├── __init__.py
│   ├── chart_service.py
│   ├── database.py
│   ├── event_service.py
│   ├── export_handlers.py
│   ├── llm_service.py
│   ├── main.py
│   ├── status_service.py
├── env
├── deisel.db
├── README.md
├── requirements.txt
├── run.py