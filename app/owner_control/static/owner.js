'use strict';
let csrf = null;
const byId = id => document.getElementById(id);
const message = text => { byId('message').textContent = text; };
const stopResult = value => ({pending: 'Ожидается', confirmed: 'Подтверждено', unavailable: 'Недоступно', uncertain: 'Неизвестно'})[value] || 'Неизвестно';
async function api(path, method = 'GET', payload = null, token = null) {
  const options = {method, credentials: 'same-origin', headers: {}};
  if (payload !== null) {options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(payload);}
  if (token !== null) options.headers['X-CSRF-Token'] = token;
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? 'Требуется вход владельца.' : `Запрос отклонён (${response.status}).`);
  return data;
}
function show(name, value) {
  const key = document.createElement('dt'); key.textContent = name;
  const detail = document.createElement('dd'); detail.textContent = value;
  byId('status').append(key, detail);
}
async function refresh() {
  const data = await api('/status', 'POST');
  byId('status').replaceChildren(); byId('audit').replaceChildren();
  show('Stop приложения', data.application_stop.active ? 'Включён' : 'Выключен');
  show('Расходы приложения (IPC)', data.application_provider.spend_suspended ? 'Приостановлены' : 'Приостановка не зафиксирована');
  show('Попытки, записанные приложением', String(data.application_provider.attempt_count));
  const usage = data.application_provider.suspension_usage;
  if (usage !== null) {
    show('Токены последней приостановки', usage.total_tokens === null ? 'Нет данных' : String(usage.total_tokens));
    show('Оценка стоимости', usage.estimated_cost === null ? 'Нет данных' : `${usage.estimated_cost} ${usage.currency || ''}`);
  }
  const worker = data.worker;
  show('Наблюдение воркера', worker.available ? `Собрано ${worker.collected_at}` : 'Недоступно');
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
    show('Фактическое использование провайдера', provider.unresolved_reservations > 0 || provider.unknown_usage_attempts > 0 ? 'Неизвестно: итоговые данные неполны; резервирование не подтверждает вызов провайдера' : 'Не подтверждается этой панелью');
  } else {
    show('Записанные токены воркера', 'Недоступно');
    show('Попытки воркера с неизвестным или неполным учётом токенов', 'Недоступно');
    show('Фактическое использование провайдера', 'Недоступно');
  }
  show('Биллинг провайдера', 'Не является источником данных этой панели');
  show('Хранилище разговоров', data.event_store.health === 'read_interface_ok' ? 'Диагностика чтения доступна' : 'Недоступно');
  show('Версия схемы разговоров', String(data.event_store.schema_version));
  show('Режим журнала', data.event_store.journal_mode);
  show('Внешние ключи', data.event_store.foreign_keys_enabled ? 'Включены' : 'Выключены');
  show('Ожидание блокировки, мс', String(data.event_store.busy_timeout_ms));
  show('Реестр приватности', data.privacy.ledger_ready ? 'Доступен' : 'Недоступен');
  for (const item of data.audit) {
    const row = document.createElement('li');
    const actions = {stop: 'включение Stop', resume: 'снятие Stop', stop_control: 'изменение Stop', worker_stop_control: 'изменение Stop воркера', session_control: 'управление сеансом', worker_stop: 'Stop воркера', application_stop: 'Stop приложения'};
    const results = {applied: 'выполнено', rejected: 'отклонено', confirmed: 'подтверждено', unavailable: 'недоступно', uncertain: 'неизвестно'};
    row.textContent = `${item.occurred_at}: ${actions[item.action_type] || 'действие владельца'}, ${results[item.result] || 'результат записан'}`;
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
    event.target.reset(); byId('login-panel').hidden = true; byId('control-panel').hidden = false;
    await refresh(); message('Вход выполнен.');
  } catch (error) {message(error.message);}
  finally {byId('login-submit').disabled = false;}
});
byId('refresh').addEventListener('click', () => refresh().catch(error => message(error.message)));
async function change(active) {
  try {
    if (!active && !confirm('Снять Stop приложения? Проверьте состояние воркера отдельно.')) return;
    const operation = await api('/operation', 'POST', null, csrf);
    await api('/stop', 'POST', {operation_id: operation.operation_id, active}, csrf);
    await refresh(); message(active ? 'Stop приложения включён.' : 'Stop приложения снят.');
  } catch (error) {message(error.message);}
}
byId('stop').addEventListener('click', () => change(true));
byId('emergency-stop').addEventListener('click', async () => {
  const button = byId('emergency-stop');
  button.disabled = true;
  byId('emergency-result').textContent = 'Приложение: ожидается; воркер: ожидается.';
  try {
    const operation = await api('/worker-stop-operation', 'POST', null, csrf);
    const result = await api('/emergency-stop', 'POST', {operation_id: operation.operation_id}, csrf);
    byId('emergency-result').textContent = `Приложение: ${stopResult(result.application)}; воркер: ${stopResult(result.worker)}.`;
    try {await refresh();}
    catch {message('Не удалось обновить состояние. Результат команды показан выше.');}
  } catch (error) {
    byId('emergency-result').textContent = 'Приложение: неизвестно; воркер: неизвестно. Проверьте состояние отдельно.';
    message(error.message);
  } finally {button.disabled = false;}
});
byId('resume').addEventListener('click', () => change(false));
byId('logout').addEventListener('click', async () => {
  try {await api('/logout', 'POST', null, csrf); csrf = null; byId('control-panel').hidden = true; byId('login-panel').hidden = false; message('Вы вышли.');}
  catch (error) {message(error.message);}
});
byId('login-help').textContent = '';
byId('login-submit').disabled = false;
