import { useEffect, useState } from 'react'

import { openSaleSocket } from '../api'
import type { SaleSnapshot } from '../api'

export type ConnectionState = 'connecting' | 'online' | 'reconnecting'

// Нарастающая пауза: частые попытки при лежащем бэкенде только мешают.
// Последнее значение повторяется, пока соединение не вернётся.
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 10000]

interface LiveState {
  saleId: string
  snapshot: SaleSnapshot | null
}

/**
 * Live-обновления остатка по WebSocket.
 *
 * Догонять состояние после разрыва не нужно: сервер присылает снимок сразу
 * при подключении, и в нём уже актуальные значения.
 */
export function useSaleLive(saleId: string): {
  snapshot: SaleSnapshot | null
  connection: ConnectionState
} {
  const [state, setState] = useState<LiveState>({ saleId, snapshot: null })
  const [connection, setConnection] = useState<ConnectionState>('connecting')

  useEffect(() => {
    let stopped = false
    let attempt = 0
    let retryTimer: number | undefined
    let disconnect: (() => void) | undefined

    const connect = () => {
      disconnect = openSaleSocket(saleId, {
        onOpen: () => {
          attempt = 0
          setConnection('online')
        },
        onSnapshot: (snapshot) => setState({ saleId, snapshot }),
        onClose: () => {
          if (stopped) return
          setConnection('reconnecting')
          const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]
          attempt += 1
          retryTimer = window.setTimeout(connect, delay)
        },
      })
    }

    connect()

    return () => {
      stopped = true
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
      disconnect?.()
    }
  }, [saleId])

  // Снимок от прошлой распродажи не показываем: сверяем id при рендере,
  // чтобы не сбрасывать состояние эффектом.
  return {
    snapshot: state.saleId === saleId ? state.snapshot : null,
    connection,
  }
}
