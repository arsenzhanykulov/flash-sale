import { useCallback, useEffect, useState } from 'react'

import { ApiError, api } from '../api'
import type { MyReservationItem } from '../api'
import { useAuth } from '../auth/authContext'
import { subscribeToCartChanges } from './cartChannel'

interface CartState {
  /** Для кого загружено: смена покупателя не должна показывать чужую корзину. */
  token: string | null
  reservations: MyReservationItem[]
  error: string | null
}

const EMPTY: MyReservationItem[] = []

/**
 * Активные брони покупателя.
 *
 * Загружается при монтировании — поэтому корзина переживает обновление
 * страницы. Обновляется по трём поводам: сообщение из другой вкладки,
 * возврат на вкладку и явный вызов `refresh` после своих действий.
 * Никакого фонового поллинга.
 */
export function useMyReservations(): {
  reservations: MyReservationItem[]
  error: string | null
  refresh: () => void
} {
  const { token, user } = useAuth()
  const [state, setState] = useState<CartState>({ token, reservations: EMPTY, error: null })

  const refresh = useCallback(() => {
    // Без токена грузить нечего, и состояние трогать не нужно: пустая корзина
    // выводится при рендере. Синхронный setState в эффекте запускал бы
    // лишний цикл рендера.
    if (token === null) return
    api
      .myReservations(token)
      .then((loaded) => setState({ token, reservations: loaded.reservations, error: null }))
      .catch((cause: unknown) =>
        setState({
          token,
          reservations: EMPTY,
          error: cause instanceof ApiError ? cause.message : 'Не удалось загрузить корзину',
        }),
      )
  }, [token])

  useEffect(refresh, [refresh])

  // Бронь, сделанную в другой вкладке, узнаём без запросов к серверу.
  useEffect(() => {
    if (user === null) return
    return subscribeToCartChanges(user.id, refresh)
  }, [user, refresh])

  // Вкладка могла спать, когда пришло сообщение, — досматриваем при возврате.
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [refresh])

  // Пока загружается ответ для нового токена, старую корзину не показываем.
  // Нет токена — корзины нет вовсе.
  const fresh = token !== null && state.token === token
  return {
    reservations: fresh ? state.reservations : EMPTY,
    error: fresh ? state.error : null,
    refresh,
  }
}
