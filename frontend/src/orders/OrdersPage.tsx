import { Link } from 'react-router-dom'

import type { Order, OrderStatus } from '../api'
import { useAuth } from '../auth/authContext'
import { formatDateTime, formatMoney } from '../format'
import { useMyOrders } from './useMyOrders'

const STATUS_LABELS: Record<OrderStatus, string> = {
  pending_payment: 'Ждёт оплаты',
  paid: 'Оплачен',
  failed: 'Оплата отклонена',
}

// Статус заказа → цвет бейджа. Переиспользуем палитру статусов распродажи:
// ждёт — жёлтый, оплачен — зелёный, отклонён — серый.
const STATUS_TONE: Record<OrderStatus, string> = {
  pending_payment: 'upcoming',
  paid: 'active',
  failed: 'ended',
}

export function OrdersPage() {
  const { user } = useAuth()
  const { orders, error } = useMyOrders()

  if (user === null) {
    return (
      <div className="panel panel--narrow">
        <h1>Мои заказы</h1>
        <p className="muted">
          Чтобы увидеть заказы, <Link to="/login">войдите</Link>.
        </p>
      </div>
    )
  }

  if (error !== null) return <p className="error">{error}</p>

  if (orders.length === 0) {
    return (
      <div className="panel">
        <h1>Мои заказы</h1>
        <p className="muted">Заказов пока нет. Загляните на витрину.</p>
      </div>
    )
  }

  return (
    <>
      <h1>Мои заказы</h1>
      <div className="orders">
        {orders.map((order) => (
          <OrderRow key={order.id} order={order} />
        ))}
      </div>
    </>
  )
}

function OrderRow({ order }: { order: Order }) {
  return (
    <article className="order">
      <div className="order__main">
        <Link to={`/sales/${order.sale_id}`} className="order__product">
          {order.product_name}
        </Link>
        <span className={`badge badge--${STATUS_TONE[order.status]}`}>
          {STATUS_LABELS[order.status]}
        </span>
      </div>
      <p className="order__amount">{formatMoney(order.amount_minor, order.currency)}</p>
      <dl className="meta">
        <div>
          <dt>Создан</dt>
          <dd>{formatDateTime(order.created_at)}</dd>
        </div>
        {order.paid_at !== null && (
          <div>
            <dt>Оплачен</dt>
            <dd>{formatDateTime(order.paid_at)}</dd>
          </div>
        )}
      </dl>
    </article>
  )
}
