import { SaleCard } from './SaleCard'
import { useSalesList } from './useSale'

export function SalesListPage() {
  const { sales, error } = useSalesList()

  if (error !== null) return <p className="error">{error}</p>
  if (sales === null) return <p className="muted">Загружаем…</p>
  if (sales.length === 0) {
    return (
      <div className="panel">
        <h1>Распродаж пока нет</h1>
        <p className="muted">
          Создайте демо-данные:{' '}
          <code>docker compose exec backend python -m app.scripts.seed</code>
        </p>
      </div>
    )
  }

  return (
    <>
      <h1>Распродажи</h1>
      <div className="grid">
        {sales.map((sale) => (
          <SaleCard key={sale.id} sale={sale} />
        ))}
      </div>
    </>
  )
}
