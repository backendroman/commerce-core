# sales-api

Ядро интернет-магазина без HTTP: каталог, цены/скидки, налоги, склады, доставка, корзина,
оплата, заказы, аккаунты, приложения с вебхуками, bulk-операции и экспорт в CSV.

## Запуск

Python 3.12+, зависимости через [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

## Пример

```python
from sales_api.channels.service import ChannelCreateInput
from sales_api.permissions import resolver
from sales_api.permissions.staff_service import StaffService
from sales_api.shop import Shop

shop = Shop()
owner_user = StaffService.create_owner(shop.context, "admin@example.com", "Qwerty12345")
owner = shop.context.db.read(lambda s: resolver.for_user(s, owner_user))
channel = shop.channels.create(owner, ChannelCreateInput(name="Web EU", currency="EUR", default_country="DE"))
```

Остальное смотрите в `tests/`.

## Структура

```
src/sales_api/
  shop.py            собирает все сервисы
  shop_context.py    часы, БД, очередь задач, реестр приложений
  persistence/       хранилище в памяти
  catalog/ channels/ inventory/ shipping/ taxes/ discounts/
  checkout/          корзина, расчёт, оформление заказа
  payments/ orders/ accounts/ permissions/
  apps/              внешние приложения и вебхуки
  bulk/              массовые операции, экспорт
```

## Заметки

- Доступ к данным только через `db.read` / `db.write` (один общий `RLock`). События из `write`
  уходят подписчикам после выхода из самой внешней транзакции, при исключении они выбрасываются.
  Откатывать изменения в `Store` нечем, поэтому сервисы сначала всё проверяют, а потом пишут.
- Платёжные, налоговые и shipping-приложения вызываются без лока, в отдельном потоке с таймаутом.
- Деньги хранятся в `Decimal` и округляются по валюте (JPY без копеек, KWD с тремя знаками).
- Время берётся только из `Clock`, в тестах подставляется `ManualClock`.
