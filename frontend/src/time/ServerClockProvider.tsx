/**
 * Серверные часы (инвариант 2, ADR-009).
 *
 * При загрузке один раз спрашиваем GET /api/time и запоминаем смещение
 * относительно часов браузера. Дальше тикаем локально — на сервер за временем
 * больше не ходим. Часам браузера не доверяем: они могут отставать на минуты,
 * и тогда таймер врал бы.
 *
 * Хук useServerClock живёт в serverClock.ts: файл с компонентом должен
 * экспортировать только компонент, иначе ломается hot reload.
 */

import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api'
import { ServerClockContext } from './serverClock'
import type { ServerClock } from './serverClock'

export function ServerClockProvider({ children }: { children: ReactNode }) {
  const [offsetMs, setOffsetMs] = useState<number | null>(null)
  const [browserNowMs, setBrowserNowMs] = useState(() => Date.now())

  useEffect(() => {
    let cancelled = false
    api
      .serverTime()
      .then(({ now }) => {
        if (!cancelled) setOffsetMs(new Date(now).getTime() - Date.now())
      })
      .catch(() => {
        // Время не получили — работаем по часам браузера, но честно
        // сообщаем об этом через synced: false.
        if (!cancelled) setOffsetMs(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const timer = setInterval(() => setBrowserNowMs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const value = useMemo<ServerClock>(
    () => ({ nowMs: browserNowMs + (offsetMs ?? 0), synced: offsetMs !== null }),
    [browserNowMs, offsetMs],
  )

  return <ServerClockContext.Provider value={value}>{children}</ServerClockContext.Provider>
}
