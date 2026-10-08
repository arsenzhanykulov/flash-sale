import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError } from '../api'
import { useAuth } from './authContext'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      await login(email)
      navigate('/')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось войти')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="panel panel--narrow">
      <h1>Вход</h1>
      <p className="muted">
        Пароля нет: укажите email. Неизвестный email станет покупателем.
      </p>
      <form onSubmit={handleSubmit} className="form">
        <label className="form__field">
          <span>Email</span>
          <input
            type="email"
            required
            value={email}
            autoComplete="email"
            placeholder="buyer1@example.com"
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <button type="submit" className="button" disabled={pending || email.length === 0}>
          {pending ? 'Входим…' : 'Войти'}
        </button>
        {error !== null && <p className="error">{error}</p>}
      </form>
    </div>
  )
}
