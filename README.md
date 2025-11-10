# TG Channel Analyst Bot (digest + redirect clicks)

Функции:
- Ежедневный дайджест из 3 пунктов с кнопками «Смотреть» (через редирект) и «Оценить» (эмодзи pos/neu/neg).
- Учёт кликов «Смотреть» по каждому пункту (честный CTR внутри поста).
- /stats N — сводка за N дней (по умолчанию 7), отправка в REPORT_CHAT_ID.
- /links <post_id> — реальные target URL и счётчики кликов.
- /export_clicks <from> <to> [post_id] — CSV за период, при превышении MAX_EXPORT_MB — ZIP.

## Установка
```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env  # заполнить значения
python main.py
```

Бот должен быть админом в канале (CHANNEL_ID) и в отчётном чате/канале (REPORT_CHAT_ID), если он задан.

## Переменные окружения
См. `.env.example`.

## Примечания
- Бот использует polling. Для VPS — можно оформить как systemd-сервис.
- Редирект-сервис (Flask) должен быть доступен снаружи по BASE_URL.
