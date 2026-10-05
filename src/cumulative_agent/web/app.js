import {statusLabels,priorityLabel,priorityTone,toLocalInput,deadlineForSave,deadlineLabel,canComplete,listQuery} from './task-utils.mjs';
import {renderResources} from './resources.mjs';
import {renderExecution} from './execution.mjs';

const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const titles = {active:'Задачи',today:'Сегодня',overdue:'Просроченные',done:'Завершённые',cancelled:'Отменённые',background:'Фоновые',resources:'Ресурсы'};
const state = {view:'active',search:'',project:'',sort:'priority',offset:0};
let authorization = '', projects = [], currentTask = null, creationKey = '', saving = false, loading = 0, editorGeneration = 0, toastTimer;

function theme() { document.body.dataset.theme = tg?.colorScheme === 'dark' ? 'dark' : 'light'; }
if (tg) { tg.ready(); tg.expand(); theme(); tg.onEvent('themeChanged',theme); tg.BackButton?.onClick(closeEditor); }
$('today-label').textContent = new Date().toLocaleDateString('ru-RU',{weekday:'long',day:'numeric',month:'long'}).toUpperCase();
$('timezone-label').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;

function toast(message, error = false) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = 'toast' + (error ? ' error' : ''); $('toast').hidden = false;
  toastTimer = setTimeout(() => $('toast').hidden = true, error ? 7000 : 3000);
}
function logout(message = '') {
  authorization = ''; loading++; closeEditor(); $('workspace').hidden = true; $('login-screen').hidden = false;
  $('task-list').replaceChildren(); $('access-token').value = '';
  $('resources-content').replaceChildren();
  $('login-message').textContent = message || 'Откройте приложение из бота или войдите с токеном доступа.';
  $('login-message').className = 'login-message' + (message ? ' error' : '');
}
async function api(path, {method = 'GET', body, ...options} = {}) {
  const response = await fetch('/api/' + path, {...options,method,credentials:'omit',headers:{Authorization:authorization,'Content-Type':'application/json'},...(body !== undefined ? {body:JSON.stringify(body)} : {})});
  let data; try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ.'); }
  if (!response.ok) {
    let message = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('; ') : data.detail || `Ошибка ${response.status}`;
    if (response.status === 401 || response.status === 403) {
      message = response.status === 403 ? 'У этой учётной записи нет доступа.' : (authorization.startsWith('Telegram ') ? 'Сессия истекла. Закройте приложение и откройте заново из бота.' : 'Неверный токен доступа.');
      logout(message);
    }
    throw new Error(message);
  }
  return data;
}
async function login(auth) {
  authorization = auth; $('login-message').textContent = 'Подключаемся…';
  try {
    await api('auth/me'); projects = await api('projects');
    const projectFilter = $('project-filter'), projectField = $('task-project');
    projectFilter.replaceChildren(new Option('Все проекты','')); projectField.replaceChildren(new Option('Без проекта',''));
    for (const project of projects) { projectFilter.add(new Option(project.id,project.id)); projectField.add(new Option(project.id,project.id)); }
    $('login-screen').hidden = true; $('workspace').hidden = false; $('access-token').value = ''; await loadTasks();
  } catch (error) { logout(error.message); }
}
$('login-form').addEventListener('submit', async event => { event.preventDefault(); const token = $('access-token').value.trim(); if (token) await login('Bearer ' + token); });
$('logout').onclick = () => logout();

function renderTasks(page) {
  const fragment = document.createDocumentFragment();
  for (const task of page.items) {
    const row = $('task-template').content.cloneNode(true), card = row.querySelector('.task-card'), content = row.querySelector('.task-content'), complete = row.querySelector('.completion-toggle');
    card.dataset.taskId = task.id;
    if (task.status === 'done') card.classList.add('completed');
    row.querySelector('.task-title').textContent = task.title;
    row.querySelector('.task-description').textContent = task.description.replace(/\s+/g,' ').slice(0,150);
    row.querySelector('.task-description').hidden = !task.description;
    const priority = row.querySelector('.priority-badge'); priority.classList.add(priorityTone(task.priority)); priority.querySelector('span').textContent = priorityLabel(task.priority); priority.title = `Приоритет: ${task.priority}`;
    const due = row.querySelector('.deadline-badge'); due.querySelector('span').textContent = deadlineLabel(task.deadline); due.classList.toggle('overdue',task.status !== 'done' && task.deadline && new Date(task.deadline) < new Date());
    const project = row.querySelector('.project-badge'); project.hidden = !task.project; project.textContent = task.project || '';
    const status = row.querySelector('.task-status'); status.textContent = statusLabels[task.status]; status.classList.add(task.status);
    content.setAttribute('aria-label',`Открыть задачу: ${task.title}`); content.onclick = () => openTask(task.id);
    complete.disabled = !canComplete(task); complete.setAttribute('aria-label',`${task.status === 'done' ? 'Вернуть в список' : 'Отметить выполненной'}: ${task.title}`);
    complete.title = task.status === 'running' ? 'Задача выполняется агентом' : task.proposal_action ? 'Нужно отдельное рассмотрение действия' : task.status === 'done' && !task.completed_manually ? 'Выполнена агентом' : complete.getAttribute('aria-label');
    complete.onclick = async () => {
      complete.disabled = true;
      try { await api(`tasks/${task.id}/control`,{method:'POST',body:{action:task.status === 'done' ? 'reopen' : 'complete'}}); toast(task.status === 'done' ? 'Задача возвращена в список' : 'Отмечена выполненной'); await loadTasks(); }
      catch (error) { complete.disabled = !canComplete(task); toast(error.message,true); }
    };
    fragment.append(row);
  }
  $('task-list').replaceChildren(fragment); $('empty-state').hidden = page.items.length > 0;
  $('empty-title').textContent = state.search || state.project ? 'Ничего не найдено' : state.view === 'overdue' ? 'Всё вовремя' : state.view === 'done' ? 'Пока нет завершённых задач' : 'Здесь пока спокойно';
  $('empty-description').textContent = state.search || state.project ? 'Попробуйте другой запрос или проект.' : state.view === 'overdue' ? 'У вас нет просроченных задач.' : 'Добавьте задачу или напоминание в свой список.';
  $('list-total').textContent = `Задач: ${page.total}`;
  for (const element of document.querySelectorAll('[data-count]')) element.textContent = page.summary[element.dataset.count] || 0;
  $('pagination').hidden = page.total <= page.limit;
  $('previous-page').disabled = page.offset === 0; $('next-page').disabled = page.next_offset === null;
  $('page-range').textContent = `${page.total ? page.offset+1 : 0}–${page.offset+page.items.length} из ${page.total}`;
  $('previous-page').onclick = () => { state.offset = Math.max(0,state.offset-page.limit); loadTasks(); };
  $('next-page').onclick = () => { if (page.next_offset !== null) { state.offset = page.next_offset; loadTasks(); } };
}
async function loadTasks() {
  if (!authorization) return;
  const resources = state.view === 'resources';
  $('mobile-resources').setAttribute('aria-pressed',String(resources));
  $('task-panel').hidden = resources; $('resource-panel').hidden = !resources;
  $('add-task').hidden = resources; $('refresh-resources').hidden = !resources;
  $('view-title').textContent = titles[state.view];
  if (resources) return loadResources();
  const generation = ++loading; $('sync-status').textContent = 'Обновляем…';
  try {
    const [page,status] = await Promise.all([api('task-list?' + listQuery(state)),api('status')]);
    if (generation !== loading || !authorization) return;
    if (page.total && state.offset >= page.total) { state.offset = 0; return loadTasks(); }
    renderTasks(page); $('sync-status').textContent = 'Обновлено'; $('agent-note').textContent = status.paused ? 'Агент на паузе' : 'Пользовательские задачи — первые';
    $('view-subtitle').textContent = status.paused ? 'Агент на паузе. Можно спокойно организовать список.' : state.view === 'background' ? 'Обслуживание проектов в свободное время.' : 'Ваши планы, в удобном порядке.';
  } catch (error) { if (generation === loading) { $('sync-status').textContent = 'Нет связи'; toast(error.message,true); } }
}
async function loadResources() {
  const generation = ++loading; $('resources-status').textContent = 'Обновляем…';
  $('view-subtitle').textContent = 'API, модели и запас для следующей задачи.';
  try {
    const data = await api('resources');
    if (generation !== loading || !authorization || state.view !== 'resources') return;
    renderResources($('resources-content'),data);
    $('resources-status').textContent = 'На ' + new Date(data.observed_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  } catch (error) { if (generation === loading && authorization) { $('resources-status').textContent = 'Не удалось обновить'; toast(error.message,true); } }
}
$('refresh-resources').onclick = loadTasks;
$('sync-provider').onclick=async()=>{try {await api('resources/sync',{method:'POST',body:{}});toast('Задача сверки поставлена в очередь');}catch(error){toast(error.message,true);}};
$('mobile-resources').onclick = () => document.querySelector('[data-view="resources"]').click();
for (const button of document.querySelectorAll('[data-view]')) button.onclick = () => {
  state.view = button.dataset.view; state.offset = 0; $('view-title').textContent = titles[state.view];
  for (const item of document.querySelectorAll('[data-view]')) item.classList.toggle('active',item === button);
  loadTasks();
};
let searchTimer;
$('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = $('search').value.trim(); state.offset = 0; loadTasks(); },250); };
$('project-filter').onchange = () => { state.project = $('project-filter').value; state.offset = 0; loadTasks(); };
$('sort').onchange = () => { state.sort = $('sort').value; state.offset = 0; loadTasks(); };

function closeEditor() { editorGeneration++; if ($('editor').open) $('editor').close(); currentTask = null; saving = false; $('save-task').disabled = false; tg?.BackButton?.hide(); }
function resetEditor(task = null) {
  currentTask = task; saving = false; $('task-form').reset(); $('save-error').textContent = ''; $('run-results').replaceChildren(); $('task-actions').replaceChildren(); $('blocked-reason').textContent = '';
  $('task-details').hidden = !task; $('new-task-options').hidden = !!task;
  $('editor-heading').textContent = task ? 'Детали задачи' : 'Новая задача'; $('save-task').textContent = task ? 'Сохранить' : 'Создать задачу'; $('save-task').disabled = false;
  $('task-title').value = task?.title || ''; $('task-description').value = task?.description || ''; $('task-project').value = task?.project || '';
  const priority = task?.priority ?? 10; $('priority-level').value = [0,10,20,30].includes(priority) ? String(priority) : 'custom'; $('priority-custom').value = priority;
  $('custom-priority-field').hidden = $('priority-level').value !== 'custom'; $('priority-custom').required = !$('custom-priority-field').hidden;
  $('task-deadline').value = toLocalInput(task?.deadline);
  $('task-recurrence').value=task?.recurrence || '';
  const alert=task?.notify_before_minutes ?? 15;
  if (![0,15,60,1440].includes(alert)) { const option=document.createElement('option');option.value=alert;option.textContent='За '+alert+' минут';$('task-alert').append(option); }
  $('task-alert').value=String(alert);
  const fixed = task && ['running','done'].includes(task.status);
  for (const field of ['task-title','task-description','task-project']) $(field).disabled = !!fixed;
  $('editor').showModal();
  tg?.BackButton?.show();
}
function newTask() { editorGeneration++; creationKey = 'miniapp:' + crypto.randomUUID(); resetEditor(); $('task-title').focus(); }
$('add-task').onclick = newTask; $('empty-add').onclick = newTask;
$('close-editor').onclick = closeEditor; $('cancel-editor').onclick = closeEditor;
$('editor').addEventListener('cancel',event => { event.preventDefault(); closeEditor(); });
$('priority-level').onchange = () => { $('custom-priority-field').hidden = $('priority-level').value !== 'custom'; $('priority-custom').required = !$('custom-priority-field').hidden; };
for (const button of document.querySelectorAll('[data-date]')) button.onclick = () => {
  if (button.dataset.date === 'clear') { $('task-deadline').value = ''; return; }
  const date = new Date(); if (button.dataset.date === 'tomorrow') date.setDate(date.getDate()+1); date.setHours(18,0,0,0);
  if (button.dataset.date === 'today' && date < new Date()) date.setHours(23,59,0,0);
  $('task-deadline').value = toLocalInput(date);
};
async function taskControl(action) {
  if (!currentTask || saving) return;
  const generation = editorGeneration, taskId = currentTask.id;
  saving = true;
  try { await api(`tasks/${taskId}/control`,{method:'POST',body:{action,reason:'task list'}}); if (generation === editorGeneration) closeEditor(); if (authorization) { toast('Список обновлён'); await loadTasks(); } }
  catch (error) { if (generation === editorGeneration) $('save-error').textContent = error.message; }
  finally { if (generation === editorGeneration) saving = false; }
}
function actionButton(label, action) { const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = label; button.onclick = () => taskControl(action); $('task-actions').append(button); }
async function openTask(id) {
  const generation = ++editorGeneration;
  try {
    const detail = await api('tasks/' + id); if (generation !== editorGeneration || !authorization) return;
    const task = detail.task; resetEditor(task);
    $('detail-status').textContent = `${statusLabels[task.status]} · ${task.type === 'user' ? 'Ваша задача' : 'Фоновая задача'} · Создана ${new Date(task.created_at).toLocaleDateString('ru-RU')}`;
    $('blocked-reason').textContent = task.blocked_reason === 'approval_required' ? 'Это действие требует отдельного разрешения. Отметить его выполненным здесь нельзя.' : task.blocked_reason?.startsWith('wait:dependencies') ? 'Ожидаем завершения связанных задач.' : task.blocked_reason?.startsWith('wait:') ? 'Агент ждёт подходящую модель, доступный ресурс или выполнение условий задачи.' : task.blocked_reason?.startsWith('manual:') ? 'Вы приостановили эту задачу.' : '';
    if (canComplete(task)) actionButton(task.status === 'done' ? 'Вернуть в список' : 'Отметить выполненной',task.status === 'done' ? 'reopen' : 'complete');
    if (!task.proposal_action && ['inbox','blocked','failed'].includes(task.status) && task.attempts < task.max_attempts) actionButton('Передать агенту','ready');
    if (task.status === 'ready') actionButton('Приостановить задачу','block');
    if (!['done','cancelled'].includes(task.status)) actionButton('Отменить выполнение','cancel');
    if (!task.proposal_action && ['done','failed','cancelled'].includes(task.status)) actionButton('Повторить новой задачей','retry');
    renderExecution($('run-results'),detail,{download:downloadArtifact,rate:rateRun});
    if (task.completed_manually) { const note = document.createElement('p'); note.className = 'field-note'; note.textContent = 'Вы отметили задачу выполненной вручную.'; $('run-results').prepend(note); }
  } catch (error) { toast(error.message,true); }
}
async function downloadArtifact(id) {
  try {
    const response=await fetch('/api/artifacts/'+id,{headers:{Authorization:authorization}});
    if(!response.ok) throw new Error('Артефакт недоступен');
    const url=URL.createObjectURL(await response.blob()), link=document.createElement('a');link.href=url;
    link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'artifact';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  } catch(error) { toast(error.message,true); }
}
async function rateRun(id,quality_score) {
  try { await api('runs/'+id+'/rating',{method:'POST',body:{quality_score}});toast('Оценка сохранена'); }
  catch(error) { toast(error.message,true); }
}
$('task-form').addEventListener('submit', async event => {
  event.preventDefault(); if (saving || !authorization) return;
  const generation = editorGeneration, task = currentTask;
  $('save-error').textContent = ''; saving = true; $('save-task').disabled = true;
  try {
    const title = $('task-title').value.trim(); if (!title) throw new Error('Добавьте название задачи.');
    const priority = Number($('priority-level').value === 'custom' ? $('priority-custom').value : $('priority-level').value);
    if (!Number.isInteger(priority) || priority < -1000 || priority > 1000) throw new Error('Приоритет должен быть целым числом от −1000 до 1000.');
    const body = {title,description:$('task-description').value,priority,project:$('task-project').value || null,deadline:deadlineForSave($('task-deadline').value,task?.deadline)};
    body.recurrence=$('task-recurrence').value || null;
    body.task_timezone=task?.task_timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    body.notify_before_minutes=Number($('task-alert').value);
    if(body.recurrence && !body.deadline) throw new Error('Для повторения задайте дедлайн.');
    if (task) {
      body.expected_updated_at = task.updated_at;
      if (['running','done'].includes(task.status)) for (const key of ['title','description','project']) delete body[key];
      await api('tasks/' + task.id,{method:'PATCH',body});
    } else {
      body.status = $('task-execution').value; body.idempotency_key = creationKey;
      body.handler=$('task-handler').value;
      if(body.handler==='coding') {body.required_capabilities=['coding'];body.max_steps=8;body.max_tokens=8192;}
      if(body.handler==='project_tests') {body.required_capabilities=[];body.max_steps=8;}
      await api('tasks',{method:'POST',body});
    }
    if (generation === editorGeneration) closeEditor();
    if (authorization) { state.offset = 0; toast('Задача сохранена'); await loadTasks(); tg?.HapticFeedback?.notificationOccurred('success'); }
  } catch (error) { if (generation === editorGeneration) $('save-error').textContent = error.message; }
  finally { if (generation === editorGeneration) { saving = false; $('save-task').disabled = false; } }
});
setInterval(async () => {
  if(!authorization || document.visibilityState!=='visible') return;
  if(!$('editor').open) return loadTasks();
  if(currentTask && !saving) {
    const generation=editorGeneration;
    try { const detail=await api('tasks/'+currentTask.id);if(generation===editorGeneration) renderExecution($('run-results'),detail,{download:downloadArtifact,rate:rateRun}); } catch {}
  }
},15000);
document.addEventListener('visibilitychange',() => { if (!document.hidden && authorization && !$('editor').open) loadTasks(); });
if (tg?.initData) { $('login-form').hidden = true; await login('Telegram ' + tg.initData); }
