import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'

import type { Sale } from '../api'
import { useAuth } from '../auth/authContext'
import { STATUS_LABELS, formatDateTime, formatMoney } from '../format'
import { Countdown } from '../time/Countdown'
import { useServerClock } from '../time/serverClock'
import { useSale } from './useSale'

/** Момент, до которого идёт отсчёт: старт для upcoming, конец для active. */
function countdownTarget(sale: Sale): string | null {
  if (sale.status === 'upcoming') return sale.start_at
  if (sale.status === 'active') return sale.end_at
  return null
}

export function SalePage() {
  const { saleId = '' } = useParams()
  const { sale, error, loading, refetch } = useSale(saleId)
  const { nowMs, synced } = useServerClock()
  const { user } = useAuth()

  // Срок по серверным часам истёк, а сервер всё ещё отдаёт прежний статус.
  // Значение выводится при рендере: отдельное состояние пришлось бы сбрасывать
  // эффектом, а это лишний цикл рендера.
  const target = sale === null ? null : countdownTarget(sale)
  const boundaryPassed = target !== null && new Date(target).getTime() <= nowMs

  // Спрашиваем сервер, пока он не сменит статус. Одного запроса мало:
  // при расхождении часов в доли секунды сервер ещё отдаёт прежний статус,
  // и локальный таймер тут же снова показал бы ноль. Как только статус
  // сменился, boundaryPassed становится false и опрос прекращается сам.
  useEffect(() => {
    if (!boundaryPassed) return
    const timer = setInterval(refetch, 1000)
    return () => clearInterval(timer)
  }, [boundaryPassed, refetch])

  if (loading) return <p className="muted">Загружаем…</p>
  if (error !== null) return <p className="error">{error}</p>
  if (sale === null) return null

  const soldOut = sale.remaining === 0
  // Кнопка включается только по статусу с сервера, никогда по локальным часам.
  const canBuy = sale.status === 'active' && !soldOut && !boundaryPassed

  return (
    <article className="panel">
      <Link to="/" className="back">
        ← Все распродажи
      </Link>

      <div className="sale">
        {sale.product_image_url !== null && (
          <img className="sale__image" src={sale.product_image_url} alt="" />
        )}

        <div className="sale__info">
          <span className={`badge badge--${sale.status}`}>{STATUS_LABELS[sale.status]}</span>
          <h1>{sale.product_name}</h1>
          {sale.product_description !== null && (
            <p className="muted">{sale.product_description}</p>
          )}

          <p className="sale__price">{formatMoney(sale.price_minor, sale.currency)}</p>

          <div className="stock">
            <div className="stock__bar">
              <div
                className="stock__fill"
                style={{ width: `${(sale.remaining / sale.quantity) * 100}%` }}
              />
            </div>
            <p className="muted">
              Осталось {sale.remaining} из {sale.quantity}
            </p>
          </div>

          {target !== null && (
            <Countdown
              target={target}
              label={sale.status === 'upcoming' ? 'До старта' : 'До конца'}
            />
          )}

          {boundaryPassed && <p className="muted">Уточняем статус у сервера…</p>}

          <dl className="meta">
            <div>
              <dt>Старт</dt>
              <dd>{formatDateTime(sale.start_at)}</dd>
            </div>
            <div>
              <dt>Конец</dt>
              <dd>{formatDateTime(sale.end_at)}</dd>
            </div>
          </dl>

          <BuyButton canBuy={canBuy} sale={sale} soldOut={soldOut} signedIn={user !== null} />

          {!synced && <p className="muted">Серверное время недоступно, таймер приблизительный.</p>}
        </div>
      </div>
    </article>
  )
}

function BuyButton({
  canBuy,
  sale,
  soldOut,
  signedIn,
}: {
  canBuy: boolean
  sale: Sale
  soldOut: boolean
  signedIn: boolean
}) {
  return (
    <>
      <button
        type="button"
        className="button button--buy"
        disabled={!canBuy}
        // Резерв появится на следующем шаге; пока кнопка только объясняет это.
        onClick={() => alert('Резерв — на следующем шаге.')}
      >
        Купить
      </button>
      {!canBuy && (
        <p className="muted">
          {soldOut
            ? 'Всё разобрали'
            : sale.status === 'ended'
              ? 'Распродажа завершена'
              : 'Кнопка включится, когда распродажа начнётся'}
        </p>
      )}
      {canBuy && !signedIn && <p className="muted">Для покупки понадобится вход.</p>}
    </>
  )
}
