import { createContext, useContext } from 'react'

import type { User } from '../api'

export interface AuthState {
  token: string | null
  user: User | null
  login: (email: string) => Promise<void>
  logout: () => void
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const auth = useContext(AuthContext)
  if (auth === null) {
    throw new Error('useAuth доступен только внутри AuthProvider')
  }
  return auth
}
