'use strict';

const form = document.getElementById('message-form');
const textarea = document.getElementById('message-content');
const sendButton = document.getElementById('send-button');
const statusLine = document.getElementById('model-status');
const actionLink = document.getElementById('status-action');
const discardButton = document.getElementById('discard-pending');
const historyList = document.getElementById('history');
const emptyState = document.getElementById('empty-state');
const conversation = document.getElementById('conversation');
const storageKey = 'ar-chat-recovery-v1';
const retentionMs = 30 * 60 * 1000;
let account = null;
let pendingMessage = null;
let requestInFlight = false;
let access = 'checking';
sendButton.disabled = true;
textarea.disabled = true;

function announce(message, kind = '', action = '') {
  statusLine.textContent = message;
  statusLine.dataset.kind = kind;
  actionLink.hidden = !action;
  if (action) {
    actionLink.href = action;
    actionLink.textContent = action === '/login' ? 'Войти снова' : 'Открыть приватность';
  }
}

function readRetained() {
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw || raw.length > 40000) { sessionStorage.removeItem(storageKey); return null; }
    const value = JSON.parse(raw);
    if (typeof value !== 'object' || value === null ||
        typeof value.account !== 'string' || typeof value.created !== 'number' ||
        !Number.isFinite(value.created) || value.created > Date.now() ||
        Date.now() - value.created > retentionMs ||
        typeof value.draft !== 'string' || value.draft.length > 8192 ||
        (value.pending !== null &&
          (typeof value.pending !== 'object' || value.pending === null ||
           typeof value.pending.id !== 'string' || !/^[\w-]{1,128}$/.test(value.pending.id) ||
           typeof value.pending.content !== 'string' || value.pending.content.length > 8192))) {
      sessionStorage.removeItem(storageKey);
      return null;
    }
    return value;
  } catch (_) { return null; }
}

function retain() {
  if (!account) return;
  try {
    if (!textarea.value && !pendingMessage) {
      sessionStorage.removeItem(storageKey);
      return;
    }
    const prior = readRetained();
    sessionStorage.setItem(storageKey, JSON.stringify({
      account, created: prior && prior.account === account ? prior.created : Date.now(),
      draft: textarea.value, pending: pendingMessage
    }));
  } catch (_) {
    announce('Не удалось сохранить черновик для восстановления. Не закрывайте вкладку.', 'error');
  }
}

function updateControls() {
  textarea.required = !pendingMessage;
  sendButton.disabled = access !== 'active' || requestInFlight ||
    (!pendingMessage && !textarea.value.trim());
  sendButton.textContent = pendingMessage ? 'Повторить отправку' : 'Отправить';
  discardButton.hidden = !pendingMessage || requestInFlight;
}

function sessionEnded() {
  access = 'expired';
  retain();
  announce('Сеанс завершён. Войдите снова, чтобы продолжить.', 'error', '/login');
  updateControls();
}

function denied() {
  access = 'denied';
  retain();
  announce('Доступ отклонён. Проверьте настройки приватности или обновите страницу.', 'error', '/privacy');
  updateControls();
}

function nearBottom() {
  return conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 180;
}

function showHistory(messages) {
  const follow = nearBottom();
  const fragment = document.createDocumentFragment();
  for (const item of messages) {
    if (item.role !== 'participant' && item.role !== 'agent') continue;
    const row = document.createElement('li');
    row.className = `message message--${item.role}`;
    const body = document.createElement('div');
    body.className = 'message__body';
    const speaker = document.createElement('span');
    speaker.className = 'message__speaker';
    speaker.textContent = item.role === 'participant' ? 'Вы' : 'Собеседник';
    const content = document.createElement('p');
    content.className = 'message__text';
    content.textContent = item.content;
    body.append(speaker, content);
    if (item.role === 'participant' && document.querySelector('a[href="/privacy"]')) {
      const source = document.createElement('a');
      source.className = 'message__source';
      source.href = `/privacy/source?ref=${encodeURIComponent(item.event_id)}`;
      source.textContent = 'Управление источником';
      body.append(source);
    }
    row.append(body);
    fragment.append(row);
  }
  historyList.replaceChildren(fragment);
  if (emptyState) emptyState.hidden = historyList.childElementCount > 0;
  if (follow) conversation.scrollTop = conversation.scrollHeight;
}

async function refreshHistory() {
  const response = await fetch('/api/history', {credentials: 'same-origin'});
  if (response.status === 401) { sessionEnded(); return false; }
  if (response.status === 403) { denied(); return false; }
  if (!response.ok) throw new Error('history unavailable');
  const data = await response.json();
  showHistory(data.messages);
  return true;
}

function resultMessage(state) {
  if (state === 'completed') return '';
  if (state === 'stopped' || state === 'skipped' || state === 'research_stopped')
    return 'Ответ остановлен. Ваше сообщение сохранено.';
  if (state === 'pending' || state === 'uncertain' || state === 'research_pending')
    return 'Сообщение сохранено. Ответ ожидается.';
  return 'Сообщение сохранено. Ответ сейчас недоступен.';
}

async function establishAccount() {
  try {
    const response = await fetch('/session', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('session unavailable');
    const session = await response.json();
    if (!session.authenticated || typeof session.username !== 'string') {
      sessionEnded();
      return;
    }
    account = session.username;
    const saved = readRetained();
    if (saved && saved.account !== account) sessionStorage.removeItem(storageKey);
    else if (saved) {
      textarea.value = saved.draft;
      pendingMessage = saved.pending;
      announce(pendingMessage
        ? 'Результат прежней отправки неизвестен. Повторите её явно с тем же номером сообщения.'
        : 'Черновик восстановлен.');
      textarea.focus();
    }
    access = 'active';
    textarea.disabled = false;
    updateControls();
  } catch (_) {
    announce('Не удалось проверить сеанс. Обновите страницу.', 'error');
  }
}

textarea.addEventListener('input', () => { retain(); updateControls(); });
discardButton.addEventListener('click', () => {
  pendingMessage = null;
  retain();
  announce('Повторная отправка отменена. Черновик сохранён.');
  updateControls();
  textarea.focus();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requestInFlight || access !== 'active') return;
  if (!pendingMessage) {
    if (!textarea.value.trim()) { textarea.focus(); return; }
    pendingMessage = {id: crypto.randomUUID(), content: textarea.value};
    retain();
  }
  const submitted = pendingMessage;
  requestInFlight = true;
  textarea.readOnly = true;
  updateControls();
  announce('Отправляем сообщение…');
  try {
    const response = await fetch('/api/messages', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
      body: JSON.stringify({message_id: submitted.id, content: submitted.content})
    });
    if (!response.ok) {
      if (response.status === 401) sessionEnded();
      else if (response.status === 403) denied();
      else if (response.status === 400 || response.status === 413 || response.status === 422) {
        pendingMessage = null;
        retain();
        announce('Сообщение не принято. Измените текст и попробуйте ещё раз. Черновик сохранён здесь.', 'error');
      } else {
        announce('Не удалось отправить. Результат неизвестен. Повторите отправку явно.', 'error');
      }
      return;
    }
    const result = await response.json();
    pendingMessage = null;
    if (textarea.value === submitted.content) textarea.value = '';
    retain();
    announce(resultMessage(result.model_state));
    try {
      await refreshHistory();
    } catch (_) {
      announce('Сообщение сохранено. Обновите страницу, чтобы увидеть его в разговоре.');
    }
  } catch (_) {
    announce('Результат отправки неизвестен. Повторная попытка проверит то же сообщение.', 'error');
  } finally {
    requestInFlight = false;
    textarea.readOnly = false;
    updateControls();
  }
});

textarea.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !event.repeat &&
      window.matchMedia('(pointer: fine)').matches) {
    event.preventDefault();
    form.requestSubmit();
  }
});

const logout = document.getElementById('logout-form');
if (logout) logout.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const response = await fetch('/logout', {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRF-Token': form.dataset.csrf}});
    if (response.ok) {
      try { sessionStorage.removeItem(storageKey); } catch (_) { /* storage unavailable */ }
      window.location.assign('/login');
    } else announce('Не удалось выйти. Сеанс может быть активен.', 'error');
  } catch (_) {
    announce('Выход не подтверждён. Проверьте сеанс перед уходом.', 'error');
  }
});

establishAccount();
