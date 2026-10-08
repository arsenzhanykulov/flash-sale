/**
 * Единственный модуль, который ходит в сеть. Компоненты импортируют только
 * отсюда — вызовов fetch в остальном коде нет.
 *
 * Адрес бэкенда берётся исключительно из VITE_API_URL (ADR-015). Значение
 * хостовое: запрос делает браузер, а он имени сервиса compose не знает.
 */

import type { LoginResponse, Sale, ServerTime } from './types'

export type { LoginResponse, Sale, SaleStatus, ServerTime, User } from './types'

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

export class ApiError extends Error {
  // Поле объявлено явно: параметр-свойства конструктора не стираются
  // при компиляции, а tsconfig включает erasableSyntaxOnly.
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  token?: string | null
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, token } = options

  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // Сеть не ответила вовсе: бэкенд не поднят или запрос заблокирован CORS.
    throw new ApiError(0, `Не удалось связаться с сервером (${API_URL})`)
  }

  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  return (await response.json()) as T
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    // 422 от FastAPI: detail — массив описаний полей.
    if (Array.isArray(detail)) return 'Проверьте введённые данные'
  } catch {
    // тело не JSON — обойдёмся кодом ответа
  }
  return `Ошибка ${response.status}`
}

export const api = {
  apiUrl: API_URL,

  login: (email: string) =>
    request<LoginResponse>('/api/auth/login', { method: 'POST', body: { email } }),

  serverTime: () => request<ServerTime>('/api/time'),

  listSales: () => request<Sale[]>('/api/sales'),

  getSale: (saleId: string) => request<Sale>(`/api/sales/${saleId}`),
}
