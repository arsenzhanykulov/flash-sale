import { useEffect, useState } from 'react'

import type { MyReservationItem } from '../api'
import { Countdown } from '../time/Countdown'
import { useServerClock } from '../time/serverClock'

// Сколько ждать, что сервер подтвердит истечение брони. Свипер просыпается
// раз в SWEEPER_INTERVAL_SECONDS, так что обычно хватает пары секунд;
// предел нужен, чтобы при недоступном бэкенде не опрашивать его вечно.
const EXPIRY_CONFIRM_LIMIT_MS = 30_000
const EXPIRY_POLL_MS = 1000

interface CartPanelProps {
  reservation: MyReservationItem
  /** Перезапросить корзину и распродажу: решение принимает сервер. */
  onExpired: () => void
  onCancel: () => void
  cancelling: boolean
  error: string | null
}

export function CartPanel({
  reservation,
  onExpired,
  onCancel,
  cancelling,
  error,
}: CartPanelProps) {
  const { nowMs } = useServerClock()
  // Срок считается по серверным часам: браузерным доверять нельзя (ADR-009).
  const expired = new Date(reservation.expires_at).getTime() <= nowMs
  const [confirmFailed, setConfirmFailed] = useState(false)

  // Ноль на отсчёте — это ещё не истечение: единицу возвращает свипер,
  // и подтвердить это может только ответ сервера.
  useEffect(() => {
    if (!expired) return
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      if (Date.now() - startedAt > EXPIRY_CONFIRM_LIMIT_MS) {
        window.clearInterval(timer)
        setConfirmFailed(true)
        return
      }
      onExpired()
    }, EXPIRY_POLL_MS)
    return () => window.clearInterval(timer)
  }, [expired, onExpired])

  return (
    <section className="cart">
      <header className="cart__header">
        <h2 className="cart__title">Ваша бронь</h2>
        <span className="badge badge--active">{reservation.status}</span>
      </header>

      {expired ? (
        <p className={confirmFailed ? 'error' : 'muted'}>
          {confirmFailed
            ? 'Не удалось обновить корзину, обновите страницу'
            : 'Бронь истекла, уточняем состояние у сервера…'}
        </p>
      ) : (
        <Countdown target={reservation.expires_at} label="Бронь держится" />
      )}

      <div className="cart__actions">
        <button
          type="button"
          className="button button--ghost"
          onClick={onCancel}
          disabled={cancelling || expired}
        >
          {cancelling ? 'Отменяем…' : 'Отменить'}
        </button>
        <button type="button" className="button" disabled title="оплата — следующий этап">
          Оплатить
        </button>
      </div>
      <p className="muted">оплата — следующий этап</p>

      {error !== null && <p className="error">{error}</p>}
    </section>
  )
}
