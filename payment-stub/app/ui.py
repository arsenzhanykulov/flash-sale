"""Страничка управления заглушкой.

Один HTML-файл без сборки: заглушка — вспомогательный сервис, и тянуть
в неё npm ради трёх кнопок несоразмерно.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Payment stub</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 1.5rem; background: #0f1115; color: #e8eaf0;
    font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; }
  .panel {
    background: #171a21; border: 1px solid #272c37; border-radius: 12px;
    padding: 1rem 1.25rem; margin-bottom: 1.25rem; max-width: 980px;
  }
  .modes { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  button {
    font: inherit; padding: 0.45rem 0.9rem; border-radius: 8px;
    border: 1px solid #272c37; background: #1e222b; color: inherit; cursor: pointer;
  }
  button:hover { border-color: #4c8dff; }
  button.active { background: #4c8dff; border-color: #4c8dff; color: #fff; font-weight: 600; }
  input[type=number] {
    font: inherit; width: 7rem; padding: 0.45rem 0.6rem; border-radius: 8px;
    border: 1px solid #272c37; background: #0f1115; color: inherit;
  }
  label { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; max-width: 980px; font-size: 0.88rem; }
  th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid #272c37; }
  th { color: #9aa3b2; font-weight: 500; font-size: 0.78rem; text-transform: uppercase; }
  code { font-size: 0.85em; color: #9aa3b2; }
  .status { font-weight: 600; }
  .status--pending { color: #c99a2e; }
  .status--succeeded { color: #2fa36b; }
  .status--declined { color: #e5484d; }
  .muted { color: #9aa3b2; font-size: 0.85rem; }
  .row-actions { display: flex; gap: 0.4rem; }
</style>
</head>
<body>
<h1>Payment stub</h1>

<section class="panel">
  <div class="modes">
    <span class="muted">Режим:</span>
    <button data-mode="approve">approve</button>
    <button data-mode="decline">decline</button>
    <button data-mode="hang">hang</button>
    <span class="muted">Задержка, мс:</span>
    <input type="number" id="delay" min="0" step="100" />
    <button id="save-delay">Применить</button>
    <label><input type="checkbox" id="duplicate" /> дублировать вебхуки</label>
  </div>
  <p class="muted" id="mode-hint"></p>
</section>

<section class="panel">
  <table>
    <thead>
      <tr>
        <th>Платёж</th><th>Ключ</th><th>Сумма</th><th>Режим</th>
        <th>Статус</th><th>Вебхуки</th><th></th>
      </tr>
    </thead>
    <tbody id="payments"></tbody>
  </table>
  <p class="muted" id="empty" hidden>Платежей пока нет.</p>
</section>

<script>
const STATUS_TEXT = { pending: 'ждёт', succeeded: 'одобрен', declined: 'отклонён' };

async function loadMode() {
  const mode = await (await fetch('/control/mode')).json();
  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.classList.toggle('active', button.dataset.mode === mode.mode);
  });
  const delay = document.getElementById('delay');
  if (document.activeElement !== delay) delay.value = mode.delay_ms;
  document.getElementById('duplicate').checked = mode.duplicate_webhooks;
  const outcome = mode.mode === 'approve' ? 'одобрены' : 'отклонены';
  document.getElementById('mode-hint').textContent =
    mode.mode === 'hang'
      ? 'Новые платежи остаются в ожидании — отпустите их кнопками ниже.'
      : `Новые платежи будут ${outcome} через ${mode.delay_ms} мс.`;
}

async function setMode(body) {
  await fetch('/control/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  await loadMode();
}

async function currentMode() {
  return (await (await fetch('/control/mode')).json()).mode;
}

async function loadPayments() {
  const payments = await (await fetch('/payments')).json();
  const tbody = document.getElementById('payments');
  document.getElementById('empty').hidden = payments.length > 0;
  tbody.innerHTML = payments
    .map((payment) => {
      const actions =
        payment.status === 'pending'
          ? `<div class="row-actions">
               <button onclick="resolve('${payment.payment_id}','approve')">одобрить</button>
               <button onclick="resolve('${payment.payment_id}','decline')">отклонить</button>
             </div>`
          : '';
      return `<tr>
        <td><code>${payment.payment_id.slice(0, 8)}</code></td>
        <td><code>${payment.idempotency_key ?? ''}</code></td>
        <td>${(payment.amount_minor / 100).toFixed(2)} ${payment.currency}</td>
        <td>${payment.mode}</td>
        <td class="status status--${payment.status}">${STATUS_TEXT[payment.status]}</td>
        <td>${payment.webhooks_sent} / ${payment.webhook_attempts}</td>
        <td>${actions}</td>
      </tr>`;
    })
    .join('');
}

async function resolve(paymentId, result) {
  await fetch(`/payments/${paymentId}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ result }),
  });
  await loadPayments();
}

document.querySelectorAll('[data-mode]').forEach((button) => {
  button.onclick = () => setMode({ mode: button.dataset.mode });
});
document.getElementById('save-delay').onclick = async () => {
  await setMode({
    mode: await currentMode(),
    delay_ms: Number(document.getElementById('delay').value),
  });
};
document.getElementById('duplicate').onchange = async (event) => {
  await setMode({ mode: await currentMode(), duplicate_webhooks: event.target.checked });
};

async function refresh() {
  await Promise.all([loadMode(), loadPayments()]);
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def control_page() -> str:
    return PAGE
