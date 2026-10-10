/**
 * Синхронизация корзины между вкладками одного покупателя.
 *
 * WebSocket распродажи публичный и не знает, чья бронь, поэтому по нему
 * вторая вкладка видит только изменившийся остаток. Дёргать
 * `/api/reservations/me` на каждое обновление остатка нельзя — нагрузка
 * росла бы как «обновления × вкладки».
 *
 * BroadcastChannel решает это без запросов к серверу вообще: сообщения
 * не выходят за пределы origin в этом браузере.
 */

const CHANNEL_NAME = 'flash-sale.cart'

interface CartChangedMessage {
  type: 'cart-changed'
  /** Чья корзина изменилась: в одном браузере могут сидеть разные покупатели. */
  userId: string
}

function openChannel(): BroadcastChannel | null {
  // Старые браузеры: молча обходимся перезапросом при возврате на вкладку.
  if (typeof BroadcastChannel === 'undefined') return null
  return new BroadcastChannel(CHANNEL_NAME)
}

/** Сообщить другим вкладкам, что бронь появилась, исчезла или истекла. */
export function announceCartChanged(userId: string): void {
  const channel = openChannel()
  if (channel === null) return
  const message: CartChangedMessage = { type: 'cart-changed', userId }
  channel.postMessage(message)
  channel.close()
}

/**
 * Подписка на изменения корзины из других вкладок.
 *
 * Своё же сообщение назад не приходит — BroadcastChannel не доставляет
 * отправителю, и это как раз нужно: вкладка-инициатор всё уже знает.
 */
export function subscribeToCartChanges(userId: string, onChanged: () => void): () => void {
  const channel = openChannel()
  if (channel === null) return () => {}

  channel.onmessage = (event: MessageEvent<CartChangedMessage>) => {
    if (event.data.type === 'cart-changed' && event.data.userId === userId) onChanged()
  }

  return () => channel.close()
}
