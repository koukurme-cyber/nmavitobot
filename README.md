# Avito Telegram Status Bot

Минималистичный Telegram-бот для двух Avito-аккаунтов:

- Агрегаты
- Авмекс

Команда:

`/status`

Бот показывает одним сообщением доступные данные по каждому аккаунту:
кошелёк, аванс, статусы объявлений и ближайший тарифный платёж.

Сообщение автоматически удаляется через 10 минут.

## BotHost

Главный файл:

`bot.py`

BotHost автоматически предоставляет Telegram-токен в переменной:

`BOT_TOKEN`

Дополнительно в BotHost нужно вручную создать четыре переменные:

`AVITO_AGGREGATY_CLIENT_ID`

`AVITO_AGGREGATY_CLIENT_SECRET`

`AVITO_AVMEX_CLIENT_ID`

`AVITO_AVMEX_CLIENT_SECRET`

Секреты в репозиторий не добавлять.

## Структура

- `bot.py` — Telegram
- `avito_provider.py` — Avito API
- `config.json` — два аккаунта
- `.env.example` — шаблон переменных
- `.gitignore`
- `README.md`

Внешних Python-библиотек нет, `requirements.txt` не требуется.
