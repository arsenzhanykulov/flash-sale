/**
 * Состояние входа (ADR-007, ADR-015).
 *
 * Токен и пользователь лежат в localStorage: при хранении только в памяти
 * вход терялся бы на каждом обновлении страницы. Компромисс — уязвимость
 * к XSS; для тестового задания принят осознанно.
 *
 * Хук useAuth живёт в authContext.ts: файл с компонентом должен экспортировать
 * только компонент, иначе ломается hot reload.
 */

import { useCallback, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api'
import type { User } from '../api'
import { AuthContext } from './authContext'
import type { AuthState } from './authContext'

const TOKEN_KEY = 'flash-sale.token'
const USER_KEY = 'flash-sale.user'

function readStoredUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (raw === null) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    // Хранилище испорчено — считаем, что входа нет.
    localStorage.removeItem(USER_KEY)
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_KEY))
  const [user, setUser] = useState<User | null>(readStoredUser)

  const login = useCallback(async (email: string) => {
    const response = await api.login(email)
    localStorage.setItem(TOKEN_KEY, response.access_token)
    localStorage.setItem(USER_KEY, JSON.stringify(response.user))
    setToken(response.access_token)
    setUser(response.user)
  }, [])

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo<AuthState>(
    () => ({ token, user, login, logout }),
    [token, user, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
