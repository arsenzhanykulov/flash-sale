/**
 * Единственный модуль, который ходит в сеть. Компоненты импортируют только
 * отсюда — вызовов fetch и WebSocket в остальном коде нет.
 *
 * Адрес бэкенда берётся исключительно из VITE_API_URL (ADR-015). Значение
 * хостовое: запрос делает браузер, а он имени сервиса compose не знает.
 */

import type { LoginResponse, MyReservations, Reservation, Sale, SaleSnapshot, ServerTime } from './types'

export type {
  CancelErrorCode,
  LoginResponse,
  MyReservationItem,
  MyReservations,
  Reservation,
  ReserveErrorCode,
  Sale,
  SaleSnapshot,
  SaleStatus,
  ServerTime,
  User,
} from './types'

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

export class ApiError extends Error {
  // Поля объявлены явно: параметр-свойства конструктора не стираются
  // при компиляции, а tsconfig включает erasableSyntaxOnly.
  readonly status: number
  /** Машинный код отказа, если сервер его прислал (резерв и отмена). */
  readonly code: string | null

  constructor(status: number, message: string, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
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
    const { message, code } = await describeError(response)
    throw new ApiError(response.status, message, code)
  }
  // 204 No Content: у отмены брони тела нет.
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function describeError(
  response: Response,
): Promise<{ message: string; code: string | null }> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    const detail = body.detail

    if (typeof detail === 'string') return { message: detail, code: null }
    // 422 от FastAPI: detail — массив описаний полей.
    if (Array.isArray(detail)) return { message: 'Проверьте введённые данные', code: null }
    // Резерв и отмена отдают объект {code, message}: у одного HTTP-кода
    // несколько причин, и ветвиться по тексту нельзя.
    if (detail !== null && typeof detail === 'object') {
      const { code, message } = detail as { code?: unknown; message?: unknown }
      return {
        message: typeof message === 'string' ? message : `Ошибка ${response.status}`,
        code: typeof code === 'string' ? code : null,
      }
    }
  } catch {
    // тело не JSON — обойдёмся кодом ответа
  }
  return { message: `Ошибка ${response.status}`, code: null }
}

export interface SaleSocketHandlers {
  onSnapshot: (snapshot: SaleSnapshot) => void
  onOpen?: () => void
  onClose?: () => void
}

/**
 * Подписка на live-обновления остатка. Возвращает функцию отписки.
 *
 * Переподключение здесь не делается: им управляет вызывающий — ему виднее,
 * с какой паузой пробовать снова и что показывать пользователю.
 */
export function openSaleSocket(saleId: string, handlers: SaleSocketHandlers): () => void {
  const socket = new WebSocket(`${API_URL.replace(/^http/, 'ws')}/ws/sales/${saleId}`)

  socket.onopen = () => handlers.onOpen?.()
  socket.onmessage = (event: MessageEvent<string>) => {
    try {
      handlers.onSnapshot(JSON.parse(event.data) as SaleSnapshot)
    } catch {
      // Мусор в кадре игнорируем: следующий снимок всё равно самодостаточен.
    }
  }
  // onerror отдельно не нужен: за ним всегда приходит onclose.
  socket.onclose = () => handlers.onClose?.()

  return () => {
    // Снимаем обработчик до close, чтобы намеренное закрытие не выглядело
    // как обрыв и не запускало переподключение.
    socket.onclose = null
    socket.close()
  }
}

export const api = {
  apiUrl: API_URL,

  login: (email: string) =>
    request<LoginResponse>('/api/auth/login', { method: 'POST', body: { email } }),

  serverTime: () => request<ServerTime>('/api/time'),

  listSales: () => request<Sale[]>('/api/sales'),

  getSale: (saleId: string) => request<Sale>(`/api/sales/${saleId}`),

  reserve: (saleId: string, token: string) =>
    request<Reservation>(`/api/sales/${saleId}/reservations`, { method: 'POST', token }),

  cancelReservation: (reservationId: string, token: string) =>
    request<void>(`/api/reservations/${reservationId}`, { method: 'DELETE', token }),

  myReservations: (token: string) => request<MyReservations>('/api/reservations/me', { token }),
}
