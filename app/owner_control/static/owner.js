'use strict';
let csrf = null;
const byId = id => document.getElementById(id);
const primaryWorker = data => data.worker.available ? data.worker : ((data.workers || []).find(entry => entry.name === 'private-model') || {}).status || data.worker;
const workerIsFresh = (worker, stale) => !stale && worker.available && Number.isFinite(Date.parse(worker.collected_at)) && Math.abs(Date.now() - Date.parse(worker.collected_at)) <= 30000;
const message = text => { byId('message').textContent = text; };
try {
  const theme = localStorage.getItem('ar-theme');
  if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
} catch (_) { /* System theme remains available. */ }
const themeLabel = () => {
  const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  byId('theme-toggle').textContent = dark ? 'Светлая тема' : 'Тёмная тема';
  return dark;
};
byId('theme-toggle').addEventListener('click', () => {
  const theme = themeLabel() ? 'light' : 'dark'; document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('ar-theme', theme); } catch (_) { /* In-memory preference. */ }
  themeLabel();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', themeLabel);
themeLabel();
function dateText(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString('ru-RU', {day:'numeric', month:'long', hour:'2-digit', minute:'2-digit'}) : 'Дата не указана';
}
const dialog = byId('owner-dialog');
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

function ask(title, description, action) {
  byId('dialog-title').textContent = title; byId('dialog-description').textContent = description;
  byId('dialog-confirm').textContent = action;
  dialog.showModal();
  return new Promise(resolve => {
    const yes = () => { dialog.returnValue = 'yes'; dialog.close(); };
    const no = () => { dialog.returnValue = ''; dialog.close(); };
    byId('dialog-confirm').addEventListener('click', yes);
    byId('dialog-cancel').addEventListener('click', no);
    dialog.addEventListener('close', () => {
      byId('dialog-confirm').removeEventListener('click', yes);
      byId('dialog-cancel').removeEventListener('click', no);
      resolve(dialog.returnValue === 'yes');
    }, {once:true});
    dialog.returnValue = '';
  });
}
function controlsVisible(visible) {
  byId('control-panel').hidden = !visible; byId('login-panel').hidden = visible;
  byId('refresh').hidden = !visible; byId('logout').hidden = !visible;
}
let snapshot = null;
let receivedAt = 0;
function tile(id, text, state) { byId(id).textContent = text; byId(id).dataset.state = state; }
function renderTiles() {
  if (!snapshot) return;
  const data = snapshot, worker = primaryWorker(data);
  const stale = Date.now() - receivedAt > 30000;
  const workerFresh = workerIsFresh(worker, stale);
  tile('application-state', stale ? 'Данные устарели' : data.application_stop.active ? 'Stop включён' : 'Stop выключен', stale ? 'unknown' : data.application_stop.active ? 'stopped' : 'observed');
  tile('worker-state', !worker.available ? 'Недоступен' : !workerFresh ? 'Данные устарели' : !worker.stop.valid ? 'Stop неизвестен' : worker.stop.active ? 'Stop включён' : 'Stop выключен', workerFresh && worker.stop.valid ? (worker.stop.active ? 'stopped' : 'observed') : 'unknown');
  byId('worker-detail').textContent = worker.available ? `Наблюдение: ${dateText(worker.collected_at)}` : 'Наблюдение воркера недоступно';
  const localSuspended = data.application_provider.spend_suspended;
  const provider = worker.provider;
  const providerKnown = workerFresh && provider.available;
  const bothSuspended = !stale && localSuspended && providerKnown && provider.spend_suspended;
  tile('spend-state', stale ? 'Данные устарели' : bothSuspended ? 'Приостановлены по учёту' : localSuspended || (providerKnown && provider.spend_suspended) ? 'Частичная приостановка' : 'Приостановка не подтверждена', bothSuspended ? 'stopped' : 'unknown');
  byId('spend-detail').textContent = `Попытки IPC: ${data.application_provider.attempt_count}; воркер: ${providerKnown ? provider.attempt_count : 'нет свежих данных'}. Биллинг недоступен.`;
  const configuredWorkers = data.workers && data.workers.length ? data.workers.map(entry => entry.status) : [worker];
  const stopped = !stale && data.application_stop.active && configuredWorkers.every(entry => workerIsFresh(entry, stale) && entry.stop.valid && entry.stop.active);
  byId('emergency-zone').hidden = stopped; byId('worker-local-stop').hidden = !stopped;
  byId('stop').setAttribute('aria-pressed', String(data.application_stop.active));
  byId('resume').setAttribute('aria-pressed', String(!data.application_stop.active));
}
setInterval(renderTiles, 1000);
const stopResult = value => ({pending: 'Ожидается', confirmed: 'Подтверждено', unavailable: 'Недоступно', uncertain: 'Неизвестно'})[value] || 'Неизвестно';
async function api(path, method = 'GET', payload = null, token = null) {
  const options = {method, credentials: 'same-origin', headers: {}};
  if (payload !== null) {options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(payload);}
  if (token !== null) options.headers['X-CSRF-Token'] = token;
  let response;
  try { response = await fetch(path, options); }
  catch (_) { throw new Error('Не удалось связаться с панелью. Статус операции неизвестен.'); }
  if (!response.ok) {
    const text = path === '/login' && response.status === 401 ? 'Неверное имя или пароль' :
      response.status === 401 ? 'Требуется вход владельца.' :
      response.status === 429 ? 'Слишком много попыток. Попробуйте позже.' :
      'Не удалось выполнить запрос. Попробуйте ещё раз.';
    throw new Error(text);
  }
  try { return await response.json(); }
  catch (_) { throw new Error('Не удалось прочитать ответ. Проверьте состояние отдельно.'); }
}
function show(name, value) {
  const key = document.createElement('dt'); key.textContent = name;
  const detail = document.createElement('dd'); detail.textContent = value;
  byId('status').append(key, detail);
}
const outcomes = {completed: 'Успешно', no_action: 'Ответ не требовался', failed: 'Сбой или неизвестный итог', invalid: 'Предложение отклонено', unsupported: 'Действие не разрешено', skipped: 'Пропущено', unavailable: 'Недоступно (причина не записана)', stopped: 'Остановлено', rejected: 'Предложение отклонено', spend_suspended: 'Расходы приостановлены', reservation: 'Резервирование без результата', rate_limited: 'Запрос ограничен провайдером', uncertain: 'Итог неизвестен', unknown: 'Итог неизвестен'};
const reasons = {invalid_proposal: 'Недопустимое предложение модели', invalid_response: 'Недопустимый ответ провайдера', refused: 'Отказ провайдера', timeout: 'Истекло время ожидания', spend_limit: 'Лимит расходов', authorization: 'Нет разрешения', provider_error: 'Ошибка провайдера', rate_limit: 'Ограничение частоты запросов провайдера', incomplete_usage: 'Учет токенов неполный', unknown: 'Причина неизвестна'};
function recentList(id, source, label) {
  const list = byId(id); list.replaceChildren();
  if (!source || !source.available) {
    const item = document.createElement('tr'); const cell = document.createElement('td');
    cell.colSpan = 5; cell.textContent = `${label}: недоступно`; item.append(cell); list.append(item); return;
  }
  if (!source.rows.length) {
    const item = document.createElement('tr'); const cell = document.createElement('td');
    cell.colSpan = 5; cell.textContent = `${label}: записей нет`; item.append(cell); list.append(item); return;
  }
  const families = {private_reply: 'Ответ приложения', provider_attempt: 'Попытка провайдера', reservation: 'Резервирование'};
  for (const row of source.rows.slice(0, 10)) {
    const item = document.createElement('tr');
    for (const text of [dateText(row.time), families[row.family] || label,
      outcomes[row.outcome] || outcomes.unknown,
      row.reason === null ? '—' : reasons[row.reason] || reasons.unknown,
      row.tokens === null ? 'Неизвестны' : String(row.tokens)]) {
      const cell = document.createElement('td'); cell.textContent = text; item.append(cell);
    }
    list.append(item);
  }
}
async function refresh() {
  const data = await api('/status', 'POST');
  snapshot = data; receivedAt = Date.now();
  byId('updated').textContent = `Обновлено в ${new Date(receivedAt).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'})}`;
  renderTiles();
  byId('status').replaceChildren(); byId('audit').replaceChildren();
  show('Stop приложения', data.application_stop.active ? 'Включён' : 'Выключен');
  show('Расходы приложения (IPC)', data.application_provider.spend_suspended ? 'Приостановлены' : 'Приостановка не зафиксирована');
  show('Попытки, записанные приложением', String(data.application_provider.attempt_count));
  const usage = data.application_provider.suspension_usage;
  if (usage !== null) {
    show('Токены последней приостановки', usage.total_tokens === null ? 'Нет данных' : String(usage.total_tokens));
    show('Оценка стоимости', usage.estimated_cost === null ? 'Нет данных' : `${usage.estimated_cost} ${usage.currency || ''}`);
  }
  const worker = primaryWorker(data);
  for (const entry of (data.workers || [])) {
    const state = entry.status;
    show(`${entry.name}: наблюдение`, state.available ? dateText(state.collected_at) : 'Недоступно');
    show(`${entry.name}: Stop`, state.stop.valid ? (state.stop.active ? 'Включён' : 'Выключен') : 'Не подтверждён');
    show(`${entry.name}: попытки`, state.provider.available ? String(state.provider.attempt_count) : 'Нет данных');
    show(`${entry.name}: записанные токены`, state.provider.available && state.provider.recorded_tokens ? String(state.provider.recorded_tokens.total_tokens) : 'Нет данных');
    show(`${entry.name}: неизвестный расход`, state.provider.available ? String(state.provider.unknown_usage_attempts) : 'Нет данных');
  }
  const publicState = data.public_runtime;
  if (publicState && publicState.available) {
    show('Публичный цикл: ритм', !publicState.cadence_observed ? 'Нет наблюдения' : publicState.cadence_seconds === null ? 'Интервал по умолчанию' : `${publicState.cadence_seconds} с`);
    for (const grant of publicState.grants) show(`Публичное разрешение ${grant.reference}`, `${grant.publications_reserved}/${grant.publication_limit} публикаций, ${grant.http_requests_reserved}/${grant.http_limit} HTTP-запросов; ${grant.revoked ? 'отозвано' : Date.parse(grant.expires_at) <= Date.now() ? 'срок истёк' : 'срок: ' + dateText(grant.expires_at)}`);
    const decisions = {no_action:'Без публикации', reply:'Ответ', propose_publication:'Предложение публикации', research:'Исследование', revise_mission:'Пересмотр направления'};
    const phases = {linked:'Сохранено', held:'Приостановлено', rejected:'Отклонено', started:'Начато', intent:'Ожидает применения'};
    for (const decision of publicState.recent) show(`Публичное решение ${decision.reference}`, `${decisions[decision.decision] || 'Неизвестное решение'}, ${phases[decision.phase] || 'Статус неизвестен'}; ${dateText(decision.time)}`);
  }
  const recent = data.recent || {};
  recentList('recent-application', recent.application, 'Ответ приложения');
  recentList('recent-worker', recent.worker, 'Попытка воркера');
  const latest = recent.application && recent.application.available && recent.application.rows[0];
  byId('application-detail').textContent = latest ? `Последнее сообщение: ${dateText(latest.time)}` : 'Время последнего ответа не записано';
  byId('latest-outcome').textContent = latest ? `Последний наблюдаемый ответ приложения: ${outcomes[latest.outcome] || outcomes.unknown}. Время относится к исходному сообщению.` : 'Последний ответ приложения: недоступен или не записан.';
  show('Наблюдение воркера', worker.available ? `Собрано ${dateText(worker.collected_at)}` : 'Недоступно');
  show('Stop воркера', worker.stop.valid ? (worker.stop.active ? 'Включён' : 'Выключен') : 'Недоступен');
  if (data.emergency_operation) {
    show('Последняя команда: приложение', stopResult(data.emergency_operation.application));
    show('Последняя команда: воркер', stopResult(data.emergency_operation.worker));
  }
  const provider = worker.provider;
  show('Приостановка расходов воркера', provider.available ? (provider.spend_suspended ? 'Приостановлены' : 'Не зафиксирована') : 'Недоступно');
  show('Попытки провайдера, записанные воркером', provider.available ? String(provider.attempt_count) : 'Недоступно');
  show('Резервирования воркера без окончательного результата', provider.available ? String(provider.unresolved_reservations) : 'Недоступно');
  if (provider.available) {
    for (const [key, label] of Object.entries({input_tokens: 'Входные токены', cached_input_tokens: 'Кэшированные входные токены', output_tokens: 'Выходные токены', total_tokens: 'Всего записанных токенов'})) {
      show(`${label} воркера`, String(provider.recorded_tokens[key]));
    }
    show('Попытки воркера с неизвестным или неполным учётом токенов', String(provider.unknown_usage_attempts));
    if (provider.unresolved_reservations > 0 || provider.unknown_usage_attempts > 0) show('Полнота учёта воркера', 'Итоговые данные неполны; резервирование не подтверждает вызов провайдера');
  } else {
    show('Записанные токены воркера', 'Недоступно');
    show('Попытки воркера с неизвестным или неполным учётом токенов', 'Недоступно');
  }
  show('Хранилище разговоров', data.event_store.health === 'read_interface_ok' ? 'Диагностика чтения доступна' : 'Недоступно');
  show('Версия схемы разговоров', String(data.event_store.schema_version));
  show('Режим журнала', ({wal: 'Журнал упреждающей записи', delete: 'Удаляемый журнал', memory: 'В памяти'})[data.event_store.journal_mode] || 'Другой режим');
  show('Внешние ключи', data.event_store.foreign_keys_enabled ? 'Включены' : 'Выключены');
  show('Ожидание блокировки, мс', String(data.event_store.busy_timeout_ms));
  show('Реестр приватности', data.privacy.ledger_ready ? 'Доступен' : 'Недоступен');
  for (const item of data.audit) {
    const row = document.createElement('li');
    const actions = {stop: 'включение Stop', resume: 'снятие Stop', stop_control: 'изменение Stop', worker_stop_control: 'изменение Stop воркера', session_control: 'управление сеансом', worker_stop: 'Stop воркера', application_stop: 'Stop приложения'};
    const results = {applied: 'выполнено', rejected: 'отклонено', confirmed: 'подтверждено', unavailable: 'недоступно', uncertain: 'неизвестно'};
    row.textContent = `${dateText(item.occurred_at)}: ${actions[item.action_type] || 'действие владельца'}, ${results[item.result] || 'результат записан'}`;
    byId('audit').append(row);
  }
}
byId('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  byId('login-submit').disabled = true;
  try {
    const challenge = await api('/login');
    const form = new FormData(event.target);
    const result = await api('/login', 'POST', {username: form.get('username'), password: form.get('password'), csrf: challenge.csrf});
    csrf = result.csrf;
    event.target.reset(); controlsVisible(true);
    await refresh(); message('Вход выполнен.');
  } catch (error) {message(error.message);}
  finally {event.target.elements.password.value = ''; byId('login-submit').disabled = false;}
});
byId('refresh').addEventListener('click', () => refresh().catch(error => message(error.message)));
async function change(active) {
  try {
    if (!active && !await ask('Снять Stop приложения?', 'Состояние воркера проверяется отдельно. Его Stop снимается только локально.', 'Снять Stop')) return;
    const operation = await api('/operation', 'POST', null, csrf);
    await api('/stop', 'POST', {operation_id: operation.operation_id, active}, csrf);
    await refresh(); message(active ? 'Stop приложения включён.' : 'Stop приложения снят.');
  } catch (error) {message(error.message);}
}
byId('stop').addEventListener('click', () => change(true));
byId('emergency-stop').addEventListener('click', async () => {
  if (!await ask(snapshot && snapshot.workers && snapshot.workers.length ? 'Остановить приложение и настроенные воркеры?' : 'Остановить приложение и воркер?', 'Уже отправленный провайдеру запрос и его стоимость не отменяются.', 'Остановить всё')) return;
  const button = byId('emergency-stop');
  button.disabled = true;
  byId('emergency-result').textContent = 'Приложение: ожидается; воркер: ожидается.';
  try {
    const operation = await api('/worker-stop-operation', 'POST', null, csrf);
    const result = await api('/emergency-stop', 'POST', {operation_id: operation.operation_id}, csrf);
    byId('emergency-result').textContent = `Приложение: ${stopResult(result.application)}; воркер: ${stopResult(result.worker)}.`;
    if (result.workers) byId('emergency-result').textContent += ' ' + Object.entries(result.workers).map(([name, outcome]) => `${name}: ${stopResult(outcome)}`).join('; ');
    try {await refresh();}
    catch {message('Не удалось обновить состояние. Результат команды показан выше.');}
  } catch (error) {
    byId('emergency-result').textContent = 'Приложение: неизвестно; воркер: неизвестно. Проверьте состояние отдельно.';
    message(error.message);
  } finally {button.disabled = false;}
});
byId('resume').addEventListener('click', () => change(false));
byId('logout').addEventListener('click', async () => {
  try {await api('/logout', 'POST', null, csrf); csrf = null; snapshot = null; controlsVisible(false); byId('updated').textContent = '';  message('Вы вышли.');}
  catch (error) {message(error.message);}
});
byId('login-submit').disabled = false;
