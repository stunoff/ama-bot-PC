# АМА РС

Техническая основа бота на Python 3.12, MySQL 8.4 и Docker Compose.

## Локальный запуск

1. Скопируйте `.env.example` в `.env` и задайте отдельные пароли для приложения и root.
2. Выполните `docker compose up --build -d --wait`. MySQL поднимется первым, затем Alembic применит миграции и запустится приложение.
3. Выполните `docker compose ps` для проверки состояния. Внутренний endpoint `/healthz` проверяет соединение приложения с MySQL.

Новые миграции можно создавать локально командой `alembic revision -m "описание"` в окружении с зависимостями из `requirements.txt`. Начальная ревизия фиксирует версию схемы. Бизнес-таблицы добавляются миграциями соответствующих модулей.

`docker compose down` останавливает сервисы. Данные MySQL хранятся в именованном томе.

## Сборка за HTTPS-прокси

Если прокси использует собственный CA, передайте доверенный PEM-бандл как секрет сборки `pip_ca`. Например, создайте локальный файл Compose вне репозитория:

```yaml
services:
  app:
    build:
      secrets: [pip_ca]
  migrate:
    build:
      secrets: [pip_ca]
secrets:
  pip_ca:
    file: /etc/ssl/certs/ca-certificates.crt
```

Укажите путь к бандлу своей среды и запустите `docker compose -f compose.yaml -f /путь/к/файлу.yaml up --build -d --wait`.
