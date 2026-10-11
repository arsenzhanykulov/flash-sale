import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError, api } from '../api'
import type { ProviderState, Sale, SaleStatus } from '../api'
import { useAuth } from '../auth/authContext'
import { CartPanel } from '../cart/CartPanel'
import { announceCartChanged } from '../cart/cartChannel'
import { useMyReservations } from '../cart/useMyReservations'
import { useMyOrders } from '../orders/useMyOrders'
import { STATUS_LABELS, formatDateTime, formatMoney } from '../format'
import { Countdown } from '../time/Countdown'
import { useServerClock } from '../time/serverClock'
import { useSale } from './useSale'
import type { ConnectionState } from './useSaleLive'
import { useSaleLive } from './useSaleLive'

/** Понятный текст на каждый машинный код отказа. */
const RESERVE_ERRORS: Record<string, string> = {
  sold_out: 'Товар разобрали — все единицы уже заняты',
  already_reserved: 'У вас уже есть бронь на эту распродажу',
  not_started: 'Распродажа ещё не началась',
  ended: 'Распродажа уже завершена',
  sale_not_found: 'Распродажа не найдена',
}

const PAY_ERRORS: Record<string, string> = {
  reservation_expired: 'Бронь истекла — единица вернулась на витрину',
  sale_ended: 'Распродажа завершена — оплату начать уже нельзя',
  reservation_cancelled: 'Бронь отменена',
  payment_failed: 'Предыдущая оплата была отклонена, бронь больше не действует',
  reservation_not_found: 'Бронь не найдена',
  sale_not_payable: 'Оплата сейчас недоступна',
}

const CANCEL_ERRORS: Record<string, string> = {
  not_cancellable: 'Бронь уже оплачивается, отменить нельзя',
  reservation_not_found: 'Бронь не найдена — возможно, она уже снята',
}

function describe(cause: unknown, texts: Record<string, string>, fallback: string): string {
  if (!(cause instanceof ApiError)) return fallback
  if (cause.code !== null && cause.code in texts) return texts[cause.code]
  return cause.message
}

/** Момент, до которого идёт отсчёт: старт для upcoming, конец для active. */
function countdownTarget(sale: Sale, status: SaleStatus): string | null {
  if (status === 'upcoming') return sale.start_at
  if (status === 'active') return sale.end_at
  return null
}

export function SalePage() {
  const { saleId = '' } = useParams()
  const navigate = useNavigate()
  const { token, user } = useAuth()

  const { sale, error, loading, refetch } = useSale(saleId)
  const { snapshot, connection } = useSaleLive(saleId)
  const { reservations, refresh: refreshCart } = useMyReservations()
  const { orders, hasPending, refresh: refreshOrders } = useMyOrders()
  const { nowMs, synced } = useServerClock()

  const [reserving, setReserving] = useState(false)
  const [reserveError, setReserveError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [expiredNotice, setExpiredNotice] = useState(false)
  const [paying, setPaying] = useState(false)
  const [payError, setPayError] = useState<string | null>(null)
  const [providerState, setProviderState] = useState<ProviderState | null>(null)

  // Изменяемую часть берём из live-снимка, когда он есть: остаток и статус
  // обновляются без перезагрузки. Товар, цена и окно неизменны и приходят
  // по HTTP один раз.
  const status = snapshot?.status ?? sale?.status ?? null
  const remaining = snapshot?.remaining ?? sale?.remaining ?? 0

  const target = sale === null || status === null ? null : countdownTarget(sale, status)
  // Срок по серверным часам истёк, а сервер всё ещё отдаёт прежний статус.
  const boundaryPassed = target !== null && new Date(target).getTime() <= nowMs

  // Смена статуса по времени (upcoming → active) счётчиков не меняет,
  // поэтому триггер молчит и live-обновление не придёт — здесь спрашиваем сами.
  useEffect(() => {
    if (!boundaryPassed) return
    const timer = setInterval(refetch, 1000)
    return () => clearInterval(timer)
  }, [boundaryPassed, refetch])

  const reservation = reservations.find((item) => item.sale_id === saleId) ?? null
  const order = orders.find((item) => item.sale_id === saleId) ?? null

  // Итог оплаты без перезагрузки: снимок по WS приходит, потому что оплата
  // меняет счётчики распродажи. Сам снимок про наш заказ ничего не знает
  // (сокет публичный), поэтому по нему перезапрашиваем свои заказы и корзину.
  // Редкий опрос как подстраховка живёт в useMyOrders.
  const snapshotAt = snapshot?.server_time ?? null
  useEffect(() => {
    if (snapshotAt === null || !hasPending) return
    refreshOrders()
    refreshCart()
  }, [snapshotAt, hasPending, refreshOrders, refreshCart])

  // Распродажа закрылась, пока у нас была корзина в held: воркер снял бронь.
  const hadHeldCart = useRef(false)
  const [closedNotice, setClosedNotice] = useState(false)
  useEffect(() => {
    if (reservation?.status === 'held') {
      hadHeldCart.current = true
      return
    }
    if (status === 'ended' && reservation === null && hadHeldCart.current) {
      hadHeldCart.current = false
      setClosedNotice(true)
    }
  }, [reservation, status])

  const handleExpired = useCallback(() => {
    setExpiredNotice(true)
    refreshCart()
    refetch()
  }, [refreshCart, refetch])

  async function handleBuy() {
    if (token === null) {
      navigate('/login')
      return
    }
    setReserveError(null)
    setExpiredNotice(false)
    setReserving(true)
    try {
      await api.reserve(saleId, token)
      refreshCart()
      if (user !== null) announceCartChanged(user.id)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        navigate('/login')
        return
      }
      setReserveError(describe(cause, RESERVE_ERRORS, 'Не удалось забронировать'))
      // Отказ почти всегда означает, что состояние изменилось — обновим.
      refetch()
      refreshCart()
    } finally {
      setReserving(false)
    }
  }

  async function handlePay() {
    if (token === null || reservation === null) return
    setPayError(null)
    setPaying(true)
    try {
      const result = await api.pay(reservation.id, token)
      setProviderState(result.provider_state)
      refreshCart()
      refreshOrders()
      if (user !== null) announceCartChanged(user.id)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        navigate('/login')
        return
      }
      setPayError(describe(cause, PAY_ERRORS, 'Не удалось начать оплату'))
      // Отказ почти всегда означает, что состояние изменилось.
      refetch()
      refreshCart()
    } finally {
      setPaying(false)
    }
  }

  async function handleCancel() {
    if (token === null || reservation === null) return
    setCancelError(null)
    setCancelling(true)
    try {
      await api.cancelReservation(reservation.id, token)
      refreshCart()
      if (user !== null) announceCartChanged(user.id)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        navigate('/login')
        return
      }
      setCancelError(describe(cause, CANCEL_ERRORS, 'Не удалось отменить бронь'))
      refreshCart()
    } finally {
      setCancelling(false)
    }
  }

  if (loading) return <p className="muted">Загружаем…</p>
  if (error !== null) return <p className="error">{error}</p>
  if (sale === null || status === null) return null

  const soldOut = remaining === 0
  // Кнопка включается только по статусу с сервера, никогда по локальным часам.
  const canBuy = status === 'active' && !soldOut && !boundaryPassed && reservation === null

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
          <div className="sale__badges">
            <span className={`badge badge--${status}`}>{STATUS_LABELS[status]}</span>
            <ConnectionIndicator state={connection} />
          </div>

          <h1>{sale.product_name}</h1>
          {sale.product_description !== null && (
            <p className="muted">{sale.product_description}</p>
          )}

          <p className="sale__price">{formatMoney(sale.price_minor, sale.currency)}</p>

          <div className="stock">
            <div className="stock__bar">
              <div
                className="stock__fill"
                style={{ width: `${(remaining / sale.quantity) * 100}%` }}
              />
            </div>
            <p className="muted">
              Осталось {remaining} из {sale.quantity}
            </p>
          </div>

          {target !== null && (
            <Countdown
              target={target}
              label={status === 'upcoming' ? 'До старта' : 'До конца'}
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

          <button
            type="button"
            className="button button--buy"
            disabled={!canBuy || reserving}
            onClick={() => void handleBuy()}
          >
            {reserving ? 'Бронируем…' : 'Купить'}
          </button>

          {reserveError !== null && <p className="error">{reserveError}</p>}
          {reservation === null && <BuyHint status={status} soldOut={soldOut} />}
          {expiredNotice && reservation === null && (
            <p className="notice">Бронь истекла — единица вернулась на витрину.</p>
          )}
          {closedNotice && (
            <p className="notice">Распродажа завершена, корзина очищена.</p>
          )}
          {reservation === null && order !== null && <OrderOutcome status={order.status} />}
          {!synced && <p className="muted">Серверное время недоступно, таймер приблизительный.</p>}
        </div>
      </div>

      {reservation !== null && (
        <CartPanel
          key={reservation.id}
          reservation={reservation}
          order={order}
          onExpired={handleExpired}
          onCancel={() => void handleCancel()}
          onPay={() => void handlePay()}
          cancelling={cancelling}
          paying={paying}
          providerState={providerState}
          error={payError ?? cancelError}
        />
      )}
    </article>
  )
}

function OrderOutcome({ status }: { status: string }) {
  if (status === 'paid') {
    return (
      <p className="notice">
        Оплачено. Заказ виден в <Link to="/orders">моих заказах</Link>.
      </p>
    )
  }
  if (status === 'failed') {
    return <p className="error">Оплата отклонена — единица вернулась на витрину.</p>
  }
  // pending_payment без брони быть не должно, но молчать тоже не стоит.
  return <p className="muted">Оплата в обработке.</p>
}

function ConnectionIndicator({ state }: { state: ConnectionState }) {
  const online = state === 'online'
  return (
    <span className={`link-state link-state--${online ? 'online' : 'offline'}`}>
      <span className="link-state__dot" />
      {online ? 'на связи' : 'переподключение'}
    </span>
  )
}

function BuyHint({ status, soldOut }: { status: SaleStatus; soldOut: boolean }) {
  if (status === 'upcoming') {
    return <p className="muted">Кнопка включится, когда распродажа начнётся</p>
  }
  if (status === 'ended') return <p className="muted">Распродажа завершена</p>
  if (soldOut) return <p className="muted">Всё разобрали</p>
  return null
}
