export type SaleStatus = 'upcoming' | 'active' | 'ended'

export interface Sale {
  id: string
  product_id: string
  product_name: string
  product_description: string | null
  product_image_url: string | null
  price_minor: number
  currency: string
  quantity: number
  /** quantity - sold - held, посчитан сервером на server_time. */
  remaining: number
  start_at: string
  end_at: string
  /** Считается на сервере — локальные часы к этому не допускаются. */
  status: SaleStatus
  server_time: string
}

/** Снимок изменяемой части распродажи, приходит по WebSocket. */
export interface SaleSnapshot {
  sale_id: string
  quantity: number
  sold: number
  held: number
  remaining: number
  status: SaleStatus
  server_time: string
}

export interface User {
  id: string
  email: string
  role: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: User
}

export interface ServerTime {
  now: string
}

export interface Reservation {
  id: string
  sale_id: string
  status: string
  expires_at: string
  created_at: string
}

export interface MyReservationItem {
  id: string
  sale_id: string
  status: string
  expires_at: string
}

export interface MyReservations {
  /** Одно время на весь ответ: по смещению от него считается остаток брони. */
  server_time: string
  reservations: MyReservationItem[]
}

/** Машинные коды отказов — по ним ветвится интерфейс, а не по тексту. */
export type ReserveErrorCode =
  | 'sale_not_found'
  | 'not_started'
  | 'ended'
  | 'sold_out'
  | 'already_reserved'

export type CancelErrorCode = 'reservation_not_found' | 'not_cancellable'

export type OrderStatus = 'pending_payment' | 'paid' | 'failed'

export interface Order {
  id: string
  sale_id: string
  product_name: string
  amount_minor: number
  currency: string
  status: OrderStatus
  created_at: string
  paid_at: string | null
}

export interface MyOrders {
  /** Одно время на весь ответ (ADR-009). */
  server_time: string
  orders: Order[]
}

/** Что удалось сделать с платёжной системой. */
export type ProviderState = 'accepted' | 'awaiting_provider'

export interface PayResult {
  order_id: string
  payment_id: string
  order_status: OrderStatus
  payment_status: string
  amount_minor: number
  currency: string
  provider_state: ProviderState
  /** true, если оплата была начата раньше: повторный клик не создал второй заказ. */
  already_started: boolean
}

export type PayErrorCode =
  | 'reservation_not_found'
  | 'reservation_expired'
  | 'reservation_cancelled'
  | 'payment_failed'
  | 'sale_ended'
  | 'sale_not_payable'
