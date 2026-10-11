import { useEffect, useState } from 'react'

import type { MyReservationItem, Order, ProviderState } from '../api'
import { formatMoney } from '../format'
import { Countdown } from '../time/Countdown'
import { useServerClock } from '../time/serverClock'

// Сколько ждать, что сервер подтвердит истечение брони. Свипер просыпается
// раз в SWEEPER_INTERVAL_SECONDS, так что обычно хватает пары секунд;
// предел нужен, чтобы при недоступном бэкенде не опрашивать его вечно.
const EXPIRY_CONFIRM_LIMIT_MS = 30_000
const EXPIRY_POLL_MS = 1000

interface CartPanelProps {
  reservation: MyReservationItem
  /** Заказ по этой брони, если оплата уже начата. */
  order: Order | null
  /** Перезапросить корзину и распродажу: решение принимает сервер. */
  onExpired: () => void
  onCancel: () => void
  onPay: () => void
  cancelling: boolean
  paying: boolean
  providerState: ProviderState | null
  error: string | null
}

export function CartPanel({
  reservation,
  order,
  onExpired,
  onCancel,
  onPay,
  cancelling,
  paying,
  providerState,
  error,
}: CartPanelProps) {
  const { nowMs } = useServerClock()
  // Бронь в paying не истекает (инвариант 4), поэтому отсчёт и кнопка отмены
  // для неё не показываются: тикающий к нулю таймер пугал бы зря.
  const inPayment = reservation.status === 'paying'
  const expired = !inPayment && new Date(reservation.expires_at).getTime() <= nowMs
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
        <span className={`badge badge--${inPayment ? 'upcoming' : 'active'}`}>
          {inPayment ? 'оплата' : reservation.status}
        </span>
      </header>

      {inPayment ? (
        <PaymentProgress providerState={providerState} order={order} />
      ) : expired ? (
        <p className={confirmFailed ? 'error' : 'muted'}>
          {confirmFailed
            ? 'Не удалось обновить корзину, обновите страницу'
            : 'Бронь истекла, уточняем состояние у сервера…'}
        </p>
      ) : (
        <Countdown target={reservation.expires_at} label="Бронь держится" />
      )}

      <div className="cart__actions">
        {!inPayment && (
          <button
            type="button"
            className="button button--ghost"
            onClick={onCancel}
            disabled={cancelling || expired || paying}
          >
            {cancelling ? 'Отменяем…' : 'Отменить'}
          </button>
        )}
        <button
          type="button"
          className="button"
          onClick={onPay}
          disabled={paying || expired || inPayment}
        >
          {paying ? 'Отправляем…' : inPayment ? 'Оплата начата' : 'Оплатить'}
        </button>
      </div>

      {order !== null && (
        <p className="muted">
          Заказ на {formatMoney(order.amount_minor, order.currency)}
        </p>
      )}
      {error !== null && <p className="error">{error}</p>}
    </section>
  )
}

function PaymentProgress({
  providerState,
  order,
}: {
  providerState: ProviderState | null
  order: Order | null
}) {
  if (providerState === 'awaiting_provider') {
    return (
      <p className="notice">
        Платёжная система недоступна — повторим автоматически. Заказ сохранён,
        оплата завершится, как только она ответит.
      </p>
    )
  }
  if (order?.status === 'failed') {
    return <p className="error">Оплата отклонена.</p>
  }
  return (
    <p className="notice">
      Ждём подтверждения оплаты. Бронь не истечёт, пока платёж в работе.
    </p>
  )
}
