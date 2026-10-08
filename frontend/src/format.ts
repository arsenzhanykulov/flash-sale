/** Деньги приходят целыми в минимальных единицах (инвариант 8). */
export function formatMoney(amountMinor: number, currency: string): string {
  const code = currency.trim().toUpperCase()
  const amount = amountMinor / 100
  try {
    return new Intl.NumberFormat('ru-RU', { style: 'currency', currency: code }).format(amount)
  } catch {
    // Неизвестный код валюты — показываем как есть, без падения интерфейса.
    return `${amount.toFixed(2)} ${code}`
  }
}

export function formatDateTime(iso: string): string {
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(iso))
}

export const STATUS_LABELS: Record<string, string> = {
  upcoming: 'Скоро',
  active: 'Идёт',
  ended: 'Завершена',
}
