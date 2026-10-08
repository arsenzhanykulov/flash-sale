/**
 * Таймер до момента `target`, считается по серверным часам.
 *
 * Компонент только показывает остаток. Факт «срок истёк» страница выводит
 * сама из серверного статуса и серверных часов — состояние распродажи
 * меняет только ответ сервера.
 */

import { useServerClock } from './serverClock'

function formatRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  const pad = (value: number) => String(value).padStart(2, '0')
  const clock = `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
  return days > 0 ? `${days} д ${clock}` : clock
}

export function Countdown({ target, label }: { target: string; label: string }) {
  const { nowMs } = useServerClock()

  return (
    <div className="countdown">
      <span className="countdown__label">{label}</span>
      <span className="countdown__value">
        {formatRemaining(new Date(target).getTime() - nowMs)}
      </span>
    </div>
  )
}
