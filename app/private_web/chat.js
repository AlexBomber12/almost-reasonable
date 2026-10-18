'use strict';

const form = document.getElementById('message-form');
const textarea = document.getElementById('message-content');
const sendButton = document.getElementById('send-button');
const statusLine = document.getElementById('model-status');
const historyList = document.getElementById('history');
const emptyState = document.getElementById('empty-state');
const conversation = document.getElementById('conversation');
let pendingMessage = null;
let requestInFlight = false;

function announce(message, kind = '') {
  statusLine.textContent = message;
  statusLine.dataset.kind = kind;
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
  if (response.status === 401 || response.status === 403) {
    announce('Сеанс завершён. Войдите снова.', 'error');
    sendButton.disabled = true;
    return false;
  }
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

if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requestInFlight) return;
  const content = textarea.value;
  if (!content.trim()) { textarea.focus(); return; }
  if (pendingMessage && pendingMessage.content !== content) {
    announce('Сначала повторите отправку прежнего сообщения. Его результат пока неизвестен.', 'error');
    return;
  }
  if (!pendingMessage) pendingMessage = {id: crypto.randomUUID(), content};
  requestInFlight = true;
  sendButton.disabled = true;
  textarea.readOnly = true;
  announce('Отправляем сообщение…');
  try {
    const response = await fetch('/api/messages', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
      body: JSON.stringify({message_id: pendingMessage.id, content: pendingMessage.content})
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        announce('Сеанс завершён. Войдите снова. Черновик остаётся на этой странице.', 'error');
        sendButton.disabled = true;
      } else {
        announce('Не удалось отправить. Черновик сохранён здесь. Попробуйте ещё раз.', 'error');
      }
      return;
    }
    const result = await response.json();
    pendingMessage = null;
    textarea.value = '';
    announce(resultMessage(result.model_state));
    try {
      await refreshHistory();
    } catch (error) {
      announce('Сообщение сохранено. Обновите страницу, чтобы увидеть его в разговоре.');
    }
  } catch (error) {
    announce('Результат отправки неизвестен. Повторная попытка проверит то же сообщение.', 'error');
  } finally {
    requestInFlight = false;
    textarea.readOnly = false;
    if (!statusLine.textContent.startsWith('Сеанс завершён')) sendButton.disabled = false;
  }
});

if (textarea) textarea.addEventListener('keydown', (event) => {
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
    if (response.ok) window.location.assign('/login');
    else announce('Не удалось выйти. Сеанс может быть активен.', 'error');
  } catch (error) {
    announce('Выход не подтверждён. Проверьте сеанс перед уходом.', 'error');
  }
});
