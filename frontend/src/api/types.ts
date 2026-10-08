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
