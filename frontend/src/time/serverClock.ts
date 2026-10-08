import { createContext, useContext } from 'react'

export interface ServerClock {
  /** Серверное время «сейчас» в миллисекундах; тикает раз в секунду. */
  nowMs: number
  /** Смещение серверных часов относительно браузерных уже измерено. */
  synced: boolean
}

export const ServerClockContext = createContext<ServerClock | null>(null)

export function useServerClock(): ServerClock {
  const clock = useContext(ServerClockContext)
  if (clock === null) {
    throw new Error('useServerClock доступен только внутри ServerClockProvider')
  }
  return clock
}
