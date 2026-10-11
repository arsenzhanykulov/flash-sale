import { useCallback, useEffect, useMemo, useState } from 'react'

import { ApiError, api } from '../api'
import type { Order } from '../api'
import { useAuth } from '../auth/authContext'
import { subscribeToCartChanges } from '../cart/cartChannel'

/**
 * Редкий опрос — только подстраховка, и только пока есть заказ в ожидании.
 *
 * Основной путь — WS-снимок распродажи: успешная и отклонённая оплата меняют
 * счётчики, поэтому триггер в БД присылает обновление почти сразу. Опрос нужен
 * на случай, когда соединение оборвалось и снимок не пришёл. На витрине без
 * незавершённых заказов запросов нет вовсе.
 */
const PENDING_POLL_MS = 10_000

const EMPTY: Order[] = []

interface OrdersState {
  token: string | null
  orders: Order[]
  error: string | null
}

export function useMyOrders(): {
  orders: Order[]
  error: string | null
  hasPending: boolean
  refresh: () => void
} {
  const { token, user } = useAuth()
  const [state, setState] = useState<OrdersState>({ token, orders: EMPTY, error: null })

  const refresh = useCallback(() => {
    // Без токена грузить нечего; пустой список выводится при рендере.
    if (token === null) return
    api
      .myOrders(token)
      .then((loaded) => setState({ token, orders: loaded.orders, error: null }))
      .catch((cause: unknown) =>
        setState({
          token,
          orders: EMPTY,
          error: cause instanceof ApiError ? cause.message : 'Не удалось загрузить заказы',
        }),
      )
  }, [token])

  useEffect(refresh, [refresh])

  // Та же шина, что у корзины: вкладка, из которой оплатили, сообщает остальным.
  useEffect(() => {
    if (user === null) return
    return subscribeToCartChanges(user.id, refresh)
  }, [user, refresh])

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [refresh])

  const fresh = token !== null && state.token === token
  const orders = fresh ? state.orders : EMPTY

  const hasPending = useMemo(
    () => orders.some((order) => order.status === 'pending_payment'),
    [orders],
  )

  // Опрос включается только при незавершённом заказе и гаснет сам, как только
  // результат пришёл — хоть по WS, хоть этим же опросом.
  useEffect(() => {
    if (!hasPending) return
    const timer = window.setInterval(refresh, PENDING_POLL_MS)
    return () => window.clearInterval(timer)
  }, [hasPending, refresh])

  return { orders, error: fresh ? state.error : null, hasPending, refresh }
}
