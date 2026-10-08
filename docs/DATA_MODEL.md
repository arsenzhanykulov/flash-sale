# Модель данных

Все id — UUID. Все время — `timestamptz` (UTC). Деньги — BIGINT в минимальных единицах (`*_minor`).
Одна бронь = одна единица товара (см. DECISIONS, ADR-008).

## users
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| email | text UNIQUE NOT NULL | хранить в lower-case |
| role | text NOT NULL | `buyer` \| `shop`, CHECK |
| created_at | timestamptz | default now() |

## products
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| shop_id | uuid FK → users | |
| name | text NOT NULL | |
| description | text | |
| image_url | text | |

## sales
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| product_id | uuid FK → products | |
| price_minor | bigint NOT NULL | CHECK > 0 |
| currency | char(3) NOT NULL | например `KGS` |
| quantity | int NOT NULL | CHECK > 0 |
| sold | int NOT NULL default 0 | счётчик оплаченных |
| held | int NOT NULL default 0 | счётчик в `held` + `paying` |
| start_at | timestamptz NOT NULL | |
| end_at | timestamptz NOT NULL | CHECK end_at > start_at |
| closed_at | timestamptz NULL | когда воркер закрыл распродажу |
| created_at | timestamptz | |

CHECK: `sold >= 0 AND held >= 0 AND sold + held <= quantity` — последняя линия защиты от оверсейла
даже при баге в коде.

Резерв:
```sql
UPDATE sales SET held = held + 1
WHERE id = :sale_id
  AND now() >= start_at AND now() < end_at AND closed_at IS NULL
  AND sold + held < quantity
RETURNING id;
```
Нет строки → «не началось / закончилось / распродано» (причину уточняем отдельным SELECT для ответа).

## reservations
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| sale_id | uuid FK → sales | |
| user_id | uuid FK → users | |
| status | text NOT NULL | `held` \| `paying` \| `paid` \| `expired` \| `failed` \| `cancelled` |
| expires_at | timestamptz NOT NULL | created_at + 10 мин; для `paying` игнорируется |
| created_at, updated_at | timestamptz | |

Индексы:
- `UNIQUE (sale_id, user_id) WHERE status IN ('held','paying','paid')` — лимит «1 на покупателя»
- `(status, expires_at)` — для свипера

Переходы: `held → paying → paid`; `held → expired` (свипер); `held → cancelled` (сам отказался
или закрытие распродажи); `paying → failed` (отказ платёжки). При `expired/failed/cancelled`
делаем `held = held - 1`; при `paid` — `held - 1, sold + 1`. Всё в одной транзакции со сменой статуса.

## orders
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| reservation_id | uuid UNIQUE FK → reservations | один заказ на бронь |
| user_id | uuid FK → users | |
| sale_id | uuid FK → sales | |
| amount_minor | bigint NOT NULL | фиксируется при создании |
| currency | char(3) | |
| status | text NOT NULL | `pending_payment` \| `paid` \| `failed` |
| created_at, paid_at | timestamptz | |

## payments
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| order_id | uuid UNIQUE FK → orders | один платёж на заказ |
| idempotency_key | text UNIQUE NOT NULL | передаётся в заглушку |
| provider_payment_id | text | id в заглушке |
| status | text NOT NULL | `pending` \| `succeeded` \| `declined` |
| last_checked_at | timestamptz | для сверки зависших |
| created_at, updated_at | timestamptz | |

## outbox
| поле | тип | примечание |
|---|---|---|
| id | uuid PK | |
| kind | text NOT NULL | `order_paid`, `sale_closed_cart_released`, … |
| dedup_key | text UNIQUE NOT NULL | например `order_paid:<order_id>` |
| recipient | text NOT NULL | |
| payload | jsonb NOT NULL | |
| status | text NOT NULL | `pending` \| `sent` \| `failed` |
| attempts | int default 0 | |
| created_at, sent_at | timestamptz | |

Вставка: `INSERT ... ON CONFLICT (dedup_key) DO NOTHING`.
