import { Link } from 'react-router-dom'

import type { Sale } from '../api'
import { STATUS_LABELS, formatMoney } from '../format'

export function SaleCard({ sale }: { sale: Sale }) {
  return (
    <Link to={`/sales/${sale.id}`} className="card">
      {sale.product_image_url !== null && (
        <img className="card__image" src={sale.product_image_url} alt="" loading="lazy" />
      )}
      <div className="card__body">
        <span className={`badge badge--${sale.status}`}>{STATUS_LABELS[sale.status]}</span>
        <h2 className="card__title">{sale.product_name}</h2>
        <p className="card__price">{formatMoney(sale.price_minor, sale.currency)}</p>
        <p className="muted">
          Осталось {sale.remaining} из {sale.quantity}
        </p>
      </div>
    </Link>
  )
}
