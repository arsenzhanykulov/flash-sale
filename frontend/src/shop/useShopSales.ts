import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, api, openSalesSocket } from '../api'
import type { SaleSnapshot, ShopSale } from '../api'
import { useAuth } from '../auth/authContext'

/**
 * Склейка перезапросов выручки. Серия продаж за это окно даёт один запрос
 * к `/api/shop/sales`, а не по запросу на каждую проданную единицу.
 */
const REVENUE_REFETCH_WINDOW_MS = 300

interface ShopState {
  token: string | null
  sales: ShopSale[]
  error: string | null
}

const EMPTY: ShopSale[] = []

/** Счётчики из снимка накладываются на строку: остальное приходит по HTTP. */
function applySnapshot(sale: ShopSale, snapshot: SaleSnapshot): ShopSale {
  return {
    ...sale,
    quantity: snapshot.quantity,
    sold: snapshot.sold,
    held: snapshot.held,
    remaining: snapshot.remaining,
    status: snapshot.status,
  }
}

export function useShopSales(): {
  sales: ShopSale[]
  error: string | null
  connection: 'connecting' | 'online' | 'reconnecting'
  forbidden: boolean
} {
  const { token } = useAuth()
  const [state, setState] = useState<ShopState>({ token, sales: EMPTY, error: null })
  const [connection, setConnection] = useState<'connecting' | 'online' | 'reconnecting'>(
    'connecting',
  )
  const [forbidden, setForbidden] = useState(false)

  const load = useCallback(() => {
    if (token === null) return
    api
      .shopSales(token)
      .then((loaded) => {
        setForbidden(false)
        setState({ token, sales: loaded.sales, error: null })
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.status === 403) {
          setForbidden(true)
          setState({ token, sales: EMPTY, error: null })
          return
        }
        setState({
          token,
          sales: EMPTY,
          error: cause instanceof ApiError ? cause.message : 'Не удалось загрузить распродажи',
        })
      })
  }, [token])

  useEffect(load, [load])

  // Распродажа, созданная при открытом дашборде, в подписку не попадёт:
  // список и подписка формируются один раз. Перечитываем при возврате
  // на вкладку — этого достаточно, пока нет события «появилась распродажа».
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') load()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [load])

  const fresh = token !== null && state.token === token
  const sales = fresh ? state.sales : EMPTY

  // Подписка пересобирается, когда меняется набор распродаж. Ключ из id,
  // чтобы изменение счётчиков не рвало соединение.
  const saleIds = sales.map((sale) => sale.id)
  const subscriptionKey = saleIds.join(',')

  // Перезапрос выручки: её нет в снимке, поэтому при изменении sold
  // перечитываем строки по HTTP — со склейкой, чтобы серия продаж
  // дала один запрос.
  const revenueTimer = useRef<number | undefined>(undefined)
  const scheduleRevenueRefetch = useCallback(() => {
    if (revenueTimer.current !== undefined) window.clearTimeout(revenueTimer.current)
    revenueTimer.current = window.setTimeout(load, REVENUE_REFETCH_WINDOW_MS)
  }, [load])

  useEffect(() => {
    if (subscriptionKey === '') return
    let stopped = false
    let attempt = 0
    let retryTimer: number | undefined
    let disconnect: (() => void) | undefined

    const connect = () => {
      disconnect = openSalesSocket(subscriptionKey.split(','), {
        onOpen: () => {
          attempt = 0
          setConnection('online')
        },
        onFrame: (frame) => {
          const snapshots = frame.type === 'snapshot' ? frame.sales : [frame.sale]
          setState((current) => {
            let soldChanged = false
            const updated = current.sales.map((sale) => {
              const snapshot = snapshots.find((item) => item.sale_id === sale.id)
              if (snapshot === undefined) return sale
              if (snapshot.sold !== sale.sold) soldChanged = true
              return applySnapshot(sale, snapshot)
            })
            // Выручка меняется только вместе с sold: по held не дёргаем.
            if (soldChanged) scheduleRevenueRefetch()
            return { ...current, sales: updated }
          })
        },
        onClose: () => {
          if (stopped) return
          setConnection('reconnecting')
          const delay = Math.min(500 * 2 ** attempt, 10_000)
          attempt += 1
          retryTimer = window.setTimeout(connect, delay)
        },
      })
    }

    connect()
    return () => {
      stopped = true
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
      if (revenueTimer.current !== undefined) window.clearTimeout(revenueTimer.current)
      disconnect?.()
    }
  }, [subscriptionKey, scheduleRevenueRefetch])

  return {
    sales,
    error: fresh ? state.error : null,
    connection,
    forbidden,
  }
}
