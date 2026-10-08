import { useCallback, useEffect, useState } from 'react'

import { ApiError, api } from '../api'
import type { Sale } from '../api'

function messageOf(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : 'Не удалось загрузить распродажу'
}

interface Loaded {
  saleId: string
  sale: Sale | null
  error: string | null
}

interface SaleState {
  sale: Sale | null
  error: string | null
  loading: boolean
  refetch: () => void
}

export function useSale(saleId: string): SaleState {
  const [loaded, setLoaded] = useState<Loaded>({ saleId, sale: null, error: null })

  const refetch = useCallback(() => {
    api
      .getSale(saleId)
      .then((sale) => setLoaded({ saleId, sale, error: null }))
      .catch((cause: unknown) => setLoaded({ saleId, sale: null, error: messageOf(cause) }))
  }, [saleId])

  useEffect(refetch, [refetch])

  // Пока в состоянии лежит результат по другому id — считаем, что грузимся.
  // Выводим это при рендере, а не сбросом состояния в эффекте: сброс запускал
  // бы лишний цикл рендера и показывал бы чужую распродажу.
  const fresh = loaded.saleId === saleId
  const sale = fresh ? loaded.sale : null
  const error = fresh ? loaded.error : null

  return { sale, error, loading: sale === null && error === null, refetch }
}

export function useSalesList() {
  const [sales, setSales] = useState<Sale[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .listSales()
      .then((loadedSales) => {
        if (!cancelled) setSales(loadedSales)
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(messageOf(cause))
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { sales, error }
}
