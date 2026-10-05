const names = {openrouter:'OpenRouter',chatgpt_codex:'ChatGPT / Codex',local:'Локальные модели'};
const capabilities = {general:'Общие задачи',coding:'Код',reasoning:'Рассуждения',vision:'Изображения',tools:'Инструменты'};
const number = value => new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(value);
const money = value => '$' + new Intl.NumberFormat('en-US',{maximumFractionDigits:6}).format(value);
const absent = value => value === null || value === undefined;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function pair(label, value) {
  const item = node('div','resource-fact');
  item.append(node('dt','',label),node('dd','',value)); return item;
}
function facts(values) { const list = node('dl','resource-facts'); for (const value of values) list.append(pair(...value)); return list; }
function badge(text, tone='muted') { return node('span','resource-state '+tone,text); }
function heading(title, state, tone='muted', mark='API') {
  const header = node('div','resource-header'), left = node('div','resource-heading');
  left.append(node('span','resource-mark',mark),node('h3','',title)); header.append(left,badge(state,tone)); return header;
}
function progress(value, limit, label) {
  const bar = node('progress','resource-progress'); bar.max = Math.max(1,limit); bar.value = Math.max(0,Math.min(value,limit)); bar.setAttribute('aria-label',label); return bar;
}
function resetLabel(model) {
  if (!model.resets_at) return 'Не указан';
  const date = new Date(model.resets_at);
  return date.toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) + (date < new Date() ? ' · срок прошёл' : '');
}
function modelRow(model, provider) {
  const row = node('article','resource-model'); row.dataset.modelId = model.id;
  const title = node('div','model-title');
  const state = !model.enabled ? ['Выключена','muted'] : !provider.adapter_available ? ['Нет подключения','muted'] : model.quota_remaining === 0 ? ['Квота исчерпана','warning'] : ['Включена','positive'];
  title.append(node('h4','',model.id),badge(...state)); row.append(title);
  row.append(node('p','model-capabilities',model.capabilities.map(x=>capabilities[x] || x).join(' · ') || 'Возможности не указаны'));
  const quota = absent(model.quota_remaining) ? 'Не отслеживается' : number(model.quota_remaining) + (absent(model.quota_limit) ? ' токенов' : ' / ' + number(model.quota_limit) + ' токенов');
  row.append(facts([['Вход / выход · за 1М токенов',money(model.input_price)+' / '+money(model.output_price)],['Остаток квоты',quota],['Сброс квоты',resetLabel(model)]]));
  if (!absent(model.quota_remaining) && !absent(model.quota_limit)) row.append(progress(model.quota_remaining,model.quota_limit,'Остаток квоты '+model.id));
  const details = node('details','model-details'); details.append(node('summary','','Все лимиты'));
  details.append(facts([['Контекст',number(model.context_size)+' токенов'],['Максимальный ответ',number(model.max_output_tokens)+' токенов'],
    ['Запросы',number(model.requests_per_minute)+' / мин'],['Токены в минуту',absent(model.tokens_per_minute) ? 'Не задано' : number(model.tokens_per_minute)],
    ['Оценка задержки',number(model.latency)+' с'],['Надёжность по реестру',number(model.reliability*100)+'%']]));
  if (model.resets_at) details.append(node('p','resource-note',model.reset_period_seconds && !absent(model.quota_limit) ? 'Автоматическое пополнение по настроенному периоду.' : 'Дата указана вручную. Автоматическое пополнение не настроено.'));
  row.append(details); return row;
}
function providerCard(provider, models, observation) {
  const card = node('section','resource-card'); card.dataset.providerId = provider.id;
  card.append(heading(names[provider.id] || provider.id,provider.adapter_available ? 'Настроен' : provider.id === 'openrouter' ? 'Нет ключа' : 'Нет адаптера',provider.adapter_available ? 'positive' : 'muted'));
  const descriptions = {openrouter:'Модели для работы с текстом, кодом и рассуждениями. Используются только модели из вашего реестра.',chatgpt_codex:'Ресурс подписки. Для выполнения задач нужен официальный адаптер; ручной учёт не даёт доступа к API.'};
  card.append(node('p','resource-description',descriptions[provider.id] || 'Провайдер моделей из вашего реестра. Доступ зависит от подключённого адаптера.'));
  card.append(facts([['Баланс',absent(provider.balance) ? 'Не указан' : money(provider.balance)],['Ограничитель запросов',absent(provider.requests_per_minute) ? 'Не задан' : number(provider.requests_per_minute)+' / мин'],['Учёт',provider.tracking === 'manual/config' ? 'Настройки и локальный учёт' : provider.tracking || 'Не указан']]));
  if(observation) {
    card.append(node('p','resource-note','Проверка API: '+(observation.observed_at ? new Date(observation.observed_at).toLocaleString('ru-RU') : 'ещё не выполнена')+(observation.status==='refresh_failed' ? ' · обновление не удалось' : '')));
    card.append(facts([['Остаток лимита API-ключа',absent(observation.key_limit_remaining) ? 'Не предоставлен' : money(observation.key_limit_remaining)],['Сброс лимита ключа',observation.key_limit_reset || 'Не предоставлен'],['Лимит запросов по API',observation.rate_limit ? number(observation.rate_limit.requests)+' / '+observation.rate_limit.interval : 'Не предоставлен']]));
    for(const model of models) {
      const live=observation.models?.[model.id];
      if(live && (Math.abs(live.input_price-model.input_price)>1e-9 || Math.abs(live.output_price-model.output_price)>1e-9)) card.append(node('p','resource-note',model.id+': цена API изменилась ('+money(live.input_price)+' / '+money(live.output_price)+' за 1М). Использование приостановлено до проверки настроек.'));
    }
  }
  if (models.length) { const list = node('div','resource-models'); for (const model of models) list.append(modelRow(model,provider)); card.append(list); }
  else card.append(node('p','resource-empty','Модели пока не добавлены.'));
  return card;
}

export function renderResources(root, data) {
  const expanded = new Set([...root.querySelectorAll('.resource-model details[open]')].map(x=>x.closest('.resource-model').dataset.modelId));
  const fragment = document.createDocumentFragment(), budgets = node('div','budget-grid');
  for (const [key,label] of [['daily','Бюджет на день'],['background','Фоновые задачи']]) {
    const value = data.budgets[key], card = node('section','budget-card');
    card.append(node('h2','',label),node('p','budget-amount',money(value.remaining)),node('p','budget-caption','доступно из '+money(value.limit)));
    card.append(progress(value.used,value.limit,'Использованный '+label.toLowerCase()));
    card.append(node('p','budget-usage','Учтено '+money(value.used)+(value.reserved > 0 ? ' · в резерве '+money(value.reserved) : '') + (value.limit === 0 ? ' · платные запросы выключены' : ' · сутки UTC'))); budgets.append(card);
  }
  fragment.append(budgets,node('h2','resource-section-title','API и модели'));
  const cards = node('div','provider-list'), providers = [...data.providers];
  if (!providers.some(x=>x.id==='openrouter')) providers.unshift({id:'openrouter',balance:null,tracking:'manual/config',requests_per_minute:null,adapter_available:false});
  providers.sort((a,b)=>a.id==='openrouter' ? -1 : b.id==='openrouter' ? 1 : a.id.localeCompare(b.id));
  for (const provider of providers) cards.append(providerCard(provider,data.models.filter(m=>m.provider===provider.id),data.observations?.[provider.id]));
  const telegram = node('section','resource-card'); telegram.dataset.providerId = 'telegram';
  telegram.append(heading('Telegram Bot API',data.telegram_ownership?.awaiting_first_user ? 'Ждёт владельца' : data.credential_readiness.telegram ? 'Настроен' : 'Не настроен',data.credential_readiness.telegram ? 'positive' : 'muted','TG'));
  telegram.append(node('p','resource-description','Приём задач в чате, команды, результаты и вход в Mini App. Модели и токены LLM не предоставляет.'));
  telegram.append(facts([['Квота провайдера','Не отслеживается'],['Стоимость в системе','Без LLM-расходов']])); cards.append(telegram); fragment.append(cards);
  const local = data.local, server = node('section','resource-card'); server.dataset.providerId='server';
  server.append(heading('Этот сервер',local.healthy ? 'В норме' : 'Нагрузка / мало ресурсов',local.healthy ? 'positive' : 'warning','CPU'));
  server.append(node('p','resource-description','Локальные отчёты, индексация файлов и обслуживание проектов. LLM-токены не расходуются.'));
  const bytes = value => absent(value) ? 'Неизвестно' : number(value/(1024**3))+' ГБ';
  server.append(facts([['CPU',number(local.cpu_count)+' ядер'],['Нагрузка · 1 мин',number(local.load_1m)],['Свободная память',bytes(local.memory.MemAvailable)],['Свободный диск',bytes(local.disk_free)]]));
  fragment.append(node('h2','resource-section-title','Выполнение'),server);
  const limits = data.execution_limits;
  const execution = node('section','resource-card compact'); execution.append(facts([['На задачу',money(limits.task_cost_limit)],['Токены на запуск',number(limits.max_tokens)],['Таймаут',number(limits.timeout_seconds)+' с'],['Агенты одновременно',number(limits.max_concurrent_agents)]])); fragment.append(execution);
  fragment.append(node('p','resource-note','Лимиты выполнения — из настроек и локального учёта. Наблюдения API имеют отдельное время проверки и не включают новые модели автоматически. Неизвестная квота не означает безлимитный API. В бюджете учтены активные резервы.'));
  root.replaceChildren(fragment);
  for (const row of root.querySelectorAll('.resource-model')) if (expanded.has(row.dataset.modelId)) row.querySelector('details').open=true;
}
