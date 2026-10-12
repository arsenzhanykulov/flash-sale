import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import type { ShopSale } from '../api'
import { useAuth } from '../auth/authContext'
import { STATUS_LABELS, formatDateTime, formatMoney } from '../format'
import { useShopSales } from './useShopSales'

export function ShopPage() {
  const { user } = useAuth()
  const { sales, error, connection, forbidden } = useShopSales()

  if (user === null) {
    return (
      <div className="panel panel--narrow">
        <h1>Магазин</h1>
        <p className="muted">
          Этот раздел для магазинов. <Link to="/login">Войдите</Link> под аккаунтом
          магазина.
        </p>
      </div>
    )
  }

  if (forbidden) {
    return (
      <div className="panel panel--narrow">
        <h1>Нет доступа</h1>
        <p className="muted">
          Раздел доступен только магазинам, а вы вошли как покупатель. Загляните
          на <Link to="/">витрину</Link> или в <Link to="/orders">свои заказы</Link>.
        </p>
      </div>
    )
  }

  if (error !== null) return <p className="error">{error}</p>

  if (sales.length === 0) {
    return (
      <div className="panel">
        <h1>Мои распродажи</h1>
        <p className="muted">
          Распродаж пока нет. Создайте демо-данные:{' '}
          <code>docker compose exec backend python -m app.scripts.seed</code>
        </p>
      </div>
    )
  }

  return (
    <>
      <div className="shop__head">
        <h1>Мои распродажи</h1>
        <span className={`link-state link-state--${connection === 'online' ? 'online' : 'offline'}`}>
          <span className="link-state__dot" />
          {connection === 'online' ? 'на связи' : 'переподключение'}
        </span>
      </div>

      <div className="table-scroll">
        <table className="shop-table">
          <thead>
            <tr>
              <th>Товар</th>
              <th>Цена</th>
              <th>Статус</th>
              <th className="num">Продано</th>
              <th className="num">В корзинах</th>
              <th className="num">Осталось</th>
              <th className="num">Не продано</th>
              <th className="num">Заказов</th>
              <th className="num">Выручка</th>
              <th>Окно</th>
            </tr>
          </thead>
          <tbody>
            {sales.map((sale) => (
              <ShopSaleRow key={sale.id} sale={sale} />
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function ShopSaleRow({ sale }: { sale: ShopSale }) {
  return (
    <tr>
      <td>
        <Link to={`/sales/${sale.id}`} className="shop-table__product">
          {sale.product_name}
        </Link>
      </td>
      <td>{formatMoney(sale.price_minor, sale.currency)}</td>
      <td>
        <span className={`badge badge--${sale.status}`}>{STATUS_LABELS[sale.status]}</span>
      </td>
      <Changing value={sale.sold} />
      <Changing value={sale.held} />
      <Changing value={`${sale.remaining} / ${sale.quantity}`} />
      <td className="num">{sale.unsold ?? '—'}</td>
      <Changing value={sale.paid_orders_count} />
      <Changing value={formatMoney(sale.revenue_minor, sale.currency)} />
      <td className="shop-table__window">
        {formatDateTime(sale.start_at)}
        <br />
        {formatDateTime(sale.end_at)}
      </td>
    </tr>
  )
}

/**
 * Ячейка, которая подсвечивается при изменении значения.
 *
 * Смена цифры в таблице иначе проходит незаметно: подсветка на секунду
 * показывает, что именно обновилось.
 */
function Changing({ value }: { value: string | number }) {
  const previous = useRef(value)
  const [flash, setFlash] = useState(false)

  useEffect(() => {
    if (previous.current === value) return
    previous.current = value
    setFlash(true)
    const timer = window.setTimeout(() => setFlash(false), 1200)
    return () => window.clearTimeout(timer)
  }, [value])

  return <td className={`num${flash ? ' cell--changed' : ''}`}>{value}</td>
}
