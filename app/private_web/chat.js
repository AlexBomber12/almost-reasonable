'use strict';

// Shared presentation initializes without session requests on surrounding pages.
(() => {
  try {
    const preference = localStorage.getItem('ar-theme');
    if (preference === 'light' || preference === 'dark') document.documentElement.dataset.theme = preference;
  } catch (_) { /* Follow system preference when storage is unavailable. */ }
  const dark = () => document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  const labels = () => document.querySelectorAll('.theme-toggle').forEach(button => {
    button.textContent = dark() ? 'Светлая тема' : 'Тёмная тема';
  });
  document.querySelectorAll('.theme-toggle').forEach(button => button.addEventListener('click', () => {
    const theme = dark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('ar-theme', theme); } catch (_) { /* In-memory choice still applies. */ }
    labels();
  }));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', labels);
  labels();
  const password = document.getElementById('password');
  const toggle = document.getElementById('password-toggle');
  if (toggle) toggle.addEventListener('click', () => {
    const visible = password.type === 'password';
    password.type = visible ? 'text' : 'password';
    toggle.textContent = visible ? 'Скрыть' : 'Показать';
    toggle.setAttribute('aria-pressed', String(visible));
  });
  const login = document.getElementById('login-form');
  if (login) login.addEventListener('submit', () => {
    const button = login.querySelector('button[type=submit]');
    button.disabled = true;
    button.textContent = 'Входим…';
  });
})();

(() => {
  const form = document.getElementById('message-form');
  if (!form) return;
  const viewport = window.visualViewport;
  if (viewport) {
    const shell = document.querySelector('.chat-shell');
    let queued = false;
    function fitViewport() {
      queued = false;
      // Native focus scrolling can pan the visible viewport independently of dvh.
      // Leave pinch zoom to the browser rather than reflowing into the zoomed area.
      if (viewport.scale !== 1) {
        for (const name of ['--chat-height', '--chat-top', '--chat-safe-bottom']) shell.style.removeProperty(name);
        return;
      }
      const reduced = viewport.height < document.documentElement.clientHeight;
      shell.style.setProperty('--chat-height', `${viewport.height}px`);
      shell.style.setProperty('--chat-top', `${viewport.offsetTop}px`);
      if (reduced) shell.style.setProperty('--chat-safe-bottom', '0px');
      else shell.style.removeProperty('--chat-safe-bottom');
    }
    function scheduleViewport() {
      if (!queued) { queued = true; requestAnimationFrame(fitViewport); }
    }
    viewport.addEventListener('resize', scheduleViewport);
    viewport.addEventListener('scroll', scheduleViewport);
    window.addEventListener('resize', scheduleViewport);
    window.addEventListener('pageshow', scheduleViewport);
    fitViewport();
  }
  const textarea = document.getElementById('message-content');
  const sendButton = document.getElementById('send-button');
  const statusLine = document.getElementById('model-status');
  const actionLink = document.getElementById('status-action');
  const discardButton = document.getElementById('cancel-retry');
  const retryActions = document.getElementById('retry-actions');
  const progress = document.getElementById('request-progress');
  const menu = document.getElementById('chat-menu');
  const menuToggle = document.getElementById('menu-toggle');
  function closeMenu(returnFocus = false) {
    menu.hidden = true; menuToggle.setAttribute('aria-expanded', 'false');
    if (returnFocus) menuToggle.focus();
  }
  menuToggle.addEventListener('click', () => {
    menu.hidden = !menu.hidden;
    menuToggle.setAttribute('aria-expanded', String(!menu.hidden));
    if (!menu.hidden) menu.querySelector('a, button').focus();
  });
  document.addEventListener('click', event => {
    if (!menu.contains(event.target) && !menuToggle.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) { event.preventDefault(); closeMenu(true); }
  });
  let optimistic = null;
  function clearOptimistic() { if (optimistic) optimistic.remove(); optimistic = null; }
  function grow() {
    textarea.rows = 1;
    const computed = getComputedStyle(textarea);
    textarea.rows = Math.min(6, Math.max(1, Math.ceil((textarea.scrollHeight - parseFloat(computed.paddingTop) - parseFloat(computed.paddingBottom)) / parseFloat(computed.lineHeight))));
  }
  function dayLabel(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return 'Дата не указана';
    const now = new Date();
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (date.toDateString() === now.toDateString()) return 'Сегодня';
    if (date.toDateString() === yesterday.toDateString()) return 'Вчера';
    return date.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long', ...(date.getFullYear() !== now.getFullYear() ? {year: 'numeric'} : {})});
  }
  const historyList = document.getElementById('history');
  const emptyState = document.getElementById('empty-state');
  const conversation = document.getElementById('conversation');
  function dateSeparator(value) {
    const separator = document.createElement('li');
    separator.className = 'date-separator'; separator.textContent = dayLabel(value);
    return separator;
  }
  // SSR uses stored UTC dates; regroup locally so midnight boundaries match refreshes.
  historyList.querySelectorAll('.date-separator').forEach(row => row.remove());
  let previousDay = '';
  for (const row of historyList.querySelectorAll('.message')) {
    const day = new Date(row.dataset.occurredAt).toDateString();
    if (day !== previousDay) {
      row.before(dateSeparator(row.dataset.occurredAt)); previousDay = day;
    }
  }
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
      actionLink.textContent = action === '/login' ? 'Войти' : 'Открыть приватность';
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
    sendButton.disabled ||= Boolean(pendingMessage);
    retryActions.hidden = !pendingMessage || requestInFlight || access !== 'active';
    progress.hidden = !requestInFlight;
  }

  function sessionEnded() {
    access = 'expired';
    retain();
    announce('Сеанс завершён', 'error', '/login');
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

  function messageRow(role, text) {
    const row = document.createElement('li');
    row.className = `message message--${role}`;
    const body = document.createElement('div'); body.className = 'message__body';
    const speaker = document.createElement('span'); speaker.className = 'visually-hidden';
    speaker.textContent = role === 'participant' ? 'Вы:' : 'Almost Reasonable:';
    const content = document.createElement('p'); content.className = 'message__text'; content.textContent = text;
    body.append(speaker, content); row.append(body); return row;
  }
  function showHistory(messages) {
    const follow = nearBottom();
    const fragment = document.createDocumentFragment();
    let previous = '';
    for (const item of messages) {
      if (item.role !== 'participant' && item.role !== 'agent') continue;
      const day = new Date(item.occurred_at).toDateString();
      if (day !== previous) {
        fragment.append(dateSeparator(item.occurred_at)); previous = day;
      }
      const row = messageRow(item.role, item.content); row.dataset.eventId = item.event_id;
      row.dataset.occurredAt = item.occurred_at; fragment.append(row);
    }
    // Replace the optimistic row atomically with acknowledged durable history.
    clearOptimistic();
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
    if (state === 'no_action') return 'Сообщение сохранено. Агент решил не отвечать.';
    if (state === 'stopped' || state === 'skipped' || state === 'research_stopped')
      return 'Ответ сейчас недоступен. Сообщение сохранено.';
    if (state === 'pending' || state === 'uncertain' || state === 'research_pending')
      return 'Сообщение сохранено. Ответ пока не получен.';
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
          ? 'Сообщение могло не отправиться'
          : 'Черновик восстановлен.', pendingMessage ? 'error' : '');
        if (!pendingMessage) setTimeout(() => {
          if (statusLine.textContent === 'Черновик восстановлен.') announce('');
        }, 3000);
        textarea.focus();
      }
      access = 'active';
      textarea.disabled = false;
      textarea.placeholder = 'Сообщение';
      grow();
      updateControls();
    } catch (_) {
      announce('Не удалось проверить сеанс. Обновите страницу.', 'error');
    }
  }

  textarea.addEventListener('input', () => { grow(); retain(); updateControls(); });
  discardButton.addEventListener('click', () => {
    pendingMessage = null;
    retain();
    announce('Повторная отправка отменена. Черновик сохранён. Уже отправленная операция могла выполниться.');
    updateControls();
    textarea.focus();
  });

  document.getElementById('retry-message').addEventListener('click', () => form.requestSubmit());

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (requestInFlight || access !== 'active') return;
    const isRetry = Boolean(pendingMessage);
    if (!pendingMessage) {
      if (!textarea.value.trim()) { textarea.focus(); return; }
      pendingMessage = {id: crypto.randomUUID(), content: textarea.value};
      retain();
    }
    const submitted = pendingMessage;
    const follow = nearBottom();
    // A retry may already be in persisted history. Only fresh submissions are optimistic.
    if (isRetry) {
      clearOptimistic();
    } else {
      optimistic = messageRow('participant', submitted.content);
      optimistic.classList.add('message--optimistic');
      const delivery = document.createElement('span'); delivery.className = 'message__delivery';
      delivery.textContent = 'Отправляется…'; optimistic.append(delivery); historyList.append(optimistic);
      if (emptyState) emptyState.hidden = true;
    }
    requestInFlight = true;
    textarea.readOnly = true;
    updateControls();
    announce('');
    if (follow) conversation.scrollTop = conversation.scrollHeight;
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
          announce('Сообщение не принято. Измените текст и попробуйте снова.', 'error');
        } else {
          announce('Не удалось отправить', 'error');
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
      announce('Не удалось отправить', 'error');
    } finally {
      clearOptimistic();
      if (emptyState) emptyState.hidden = historyList.childElementCount > 0;
      requestInFlight = false;
      textarea.readOnly = false;
      grow();
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

})();
