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

## Подключение предприятия

Первичное подключение выполняет оператор с доступом к Docker. Подготовьте локальный
JSON и передайте его через stdin:

```sh
docker compose exec -T app python -m ama_rs.setup_enterprise save /dev/stdin --revision 0 --operator admin < enterprise.json
docker compose exec -T app python -m ama_rs.setup_enterprise show branch-1
```

Минимальный пример файла:

```json
{
  "id": "branch-1",
  "iiko_id": "iiko-100",
  "name": "Предприятие 1",
  "format": "delivery",
  "timezone": "Europe/Moscow",
  "server_address": "server.example.invalid",
  "lm_address": "lm.example.invalid",
  "contact_mode": "independent",
  "final_route": "headquarters",
  "brands": ["Бренд 1"],
  "chats": {
    "enterprise": {
      "platform": "telegram",
      "id": "-100111",
      "kind": "enterprise",
      "calendar": [{"weekday": 0, "start": "09:00", "end": "21:00"}]
    }
  },
  "grants": [{"chat": "enterprise", "role": "service_account", "user": "123456"}]
}
```

Целевые чаты задаются отдельными ключами даже при общем чате для нескольких
предприятий. Все идентификаторы пользователей, чатов и тем передаются строками.
В календарях недели понедельник имеет номер 0. Время окончания раньше начала
означает переход через полночь. Для круглосуточного чата укажите `"always"`.
Пустой или отсутствующий календарь закрыт для автоматической доставки.
Для замещений можно задать границы назначения `starts_at` и `ends_at`
в ISO 8601 с часовым поясом. Для обновления укажите текущий номер ревизии.
