'use strict';
(() => {
const feedback = document.getElementById('privacy-feedback');
const toastKey = 'ar-privacy-feedback';
const notices = {
  granted: 'Публикация разрешена. Срок указан в списке разрешений.',
  revoked: 'Разрешение отозвано',
  applied: 'Сообщение удалено',
  pending: 'Удаление ожидает выполнения',
  incomplete_blocked: 'Удаление не завершено. Его можно повторить в настройках приватности.',
  reconciled: 'Проверка незавершённых операций выполнена. Проверьте статус нужного удаления.',
  blocked: 'Удаление пока не завершено. Проверьте статус в настройках приватности.'
};
function show(text, error = false) {
  feedback.textContent = text;
  feedback.className = error ? 'feedback' : 'toast';
}
try {
  const code = sessionStorage.getItem(toastKey);
  sessionStorage.removeItem(toastKey);
  if (notices[code]) show(notices[code]);
} catch (_) { /* Operation feedback still works without storage. */ }
function finish(code) {
  const text = notices[code] || 'Операция выполнена. Проверьте статус в настройках приватности.';
  show(text);
  try {
    sessionStorage.setItem(toastKey, code);
    location.assign('/privacy');
  } catch (_) {
    const link = document.createElement('a'); link.href = '/privacy';
    link.textContent = 'Открыть приватность'; feedback.append(' ', link);
  }
}
const purpose = document.getElementById('purpose-help');
if (purpose) document.querySelectorAll('input[name=purpose]').forEach(input => input.addEventListener('change', () => {
  purpose.textContent = input.value === 'identifiable_disclosure'
    ? 'Агент сможет публично упомянуть, что мысль ваша, в связи с этим сообщением.'
    : 'Агент сможет дословно процитировать это сообщение. Цитата может быть узнаваемой.';
}));
const dialog = document.getElementById('delete-dialog');
let deletionForm = null;
if (dialog) {
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const buttons = [...dialog.querySelectorAll('button:not(:disabled)')];
    const first = buttons[0], last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  document.getElementById('delete-cancel').addEventListener('click', () => dialog.close());
  document.getElementById('delete-confirm').addEventListener('click', () => {
    dialog.close(); if (deletionForm) submit(deletionForm);
  });
}
async function submit(form) {
  if (form.dataset.busy === 'true') return;
  const data = Object.fromEntries(new FormData(form));
  const endpoint = form.dataset.endpoint;
  if (endpoint !== '/api/privacy/notice' && endpoint !== '/api/privacy/reconcile') {
    data.operation_id = form.dataset.operationId ||= crypto.randomUUID();
  }
  if (data.validity_hours) data.validity_hours = Number(data.validity_hours);
  const button = form.querySelector('button');
  button.disabled = true; form.dataset.busy = 'true';
  show('Выполняем…');
  try {
    const response = await fetch(endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('main').dataset.csrf},
      body: JSON.stringify(data)
    });
    if (!response.ok) {
      show(response.status === 401 ? 'Сеанс завершён. Войдите снова.' :
        'Не удалось выполнить. Попробуйте ещё раз.', true);
      return;
    }
    const result = await response.json();
    if (endpoint === '/api/privacy/notice') { location.assign('/chat'); return; }
    if (endpoint === '/api/privacy/grants') finish('granted');
    else if (endpoint === '/api/privacy/revocations') finish('revoked');
    else finish(result.status);
  } catch (_) {
    show('Статус операции неизвестен. Повторите попытку.', true);
  } finally {
    button.disabled = false; form.dataset.busy = 'false';
  }
}
document.querySelectorAll('form[data-endpoint]').forEach(form => {
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (form.dataset.endpoint === '/api/privacy/deletions') {
      deletionForm = form; dialog.showModal();
    } else submit(form);
  });
});
})();
