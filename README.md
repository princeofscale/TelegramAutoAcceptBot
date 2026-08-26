# Автоприём заявок в Telegram-каналы

Один общий бот: владелец добавляет его администратором в свой приватный канал,
включает тумблер — заявки принимаются автоматически, статистика копится.

## Что делает

- Автоприём заявок (`chat_join_request` → `approveChatJoinRequest`)
- Статистика: заявки, принято, отписалось, **разбивка по инвайт-ссылкам**,
  доля Premium и доля свежих аккаунтов
- Русский и английский, язык берётся из клиента и переключается кнопкой
- Алерты владельцу, если бота удалили из канала или сняли права

Подключить канал может **только владелец** — если бота добавил администратор,
бот выходит из канала и пишет об этом.

## Ограничение, которое не обойти

Bot API **не даёт способа прочитать уже висящие заявки** — метод есть только в
MTProto и ботам запрещён. Бот видит заявки, поданные после его подключения.
Старые надо одобрить один раз руками.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pybabel compile -d locales -D messages

cp .env.example .env    # BOT_TOKEN от @BotFather, HASH_SALT сгенерировать
.venv/bin/python bot.py
```

`HASH_SALT` задаётся один раз и **никогда не меняется**: идентификаторы
заявителей хранятся как `HMAC(user_id, соль)`, и при смене соли вступления
перестанут сопоставляться с отписками — статистика отписок обнулится.

Самопроверка: `.venv/bin/python test_db.py`

## Деплой на Ubuntu

```ini
# /etc/systemd/system/approvebot.service
[Unit]
Description=Telegram auto-approve bot
After=network-online.target

[Service]
WorkingDirectory=/opt/approvebot
EnvironmentFile=/opt/approvebot/.env
ExecStart=/opt/approvebot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now approvebot
journalctl -u approvebot -f
```

Обновление: `git pull && systemctl restart approvebot`.

Простой до суток безопасен — Telegram хранит апдейты 24 часа и при старте бот
разберёт накопившиеся заявки сам. Дольше суток заявки теряются безвозвратно
(прочитать их нечем, см. выше).

## Правка текстов

Строки живут в коде внутри `_()`. После изменения:

```bash
.venv/bin/pybabel extract -F babel.cfg -o locales/messages.pot .
.venv/bin/pybabel update -i locales/messages.pot -d locales -D messages
# заполнить msgstr в locales/*/LC_MESSAGES/messages.po
.venv/bin/pybabel compile -d locales -D messages
```

## Что сознательно не сделано

| | Когда добавлять |
|---|---|
| Mini App с графиками | После недели работы на реальном канале — когда станет видно, каких цифр не хватает в таблице |
| Капча | Когда появится названный противник. Кнопка накрутку не останавливает; ловит её аномалия скорости заявок + доля свежих аккаунтов, которая уже в отчёте |
| Тарифы | При появлении пользователей. `ALTER TABLE channels ADD COLUMN plan` — одна строка |
| Приветственные сообщения | Осторожно: массовая рассылка в личку от общего бота — это жалобы на спам и бан бота вместе со всеми подключёнными каналами |
| Postgres | Когда бот и веб-часть разъедутся по разным машинам |
