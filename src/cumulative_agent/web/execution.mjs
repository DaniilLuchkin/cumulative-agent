const stages = {context:'Чтение выбранных файлов',worktree:'Рабочая копия',analysis:'Анализ',model:'Запрос к модели',patch:'Подготовка patch',tests:'Проверка тестами',review:'Ревью',local:'Локальная проверка',finished:'Завершение'};
const statuses = {done:'Готово',passed:'Пройдено',running:'Выполняется',pending:'Требует решения',unverified:'Не подтверждено',failed:'Ошибка',cancelled:'Отменено',yielded:'Фон приостановлен'};
const money = n => '$' + Number(n || 0).toFixed(6);
function node(tag,text,className='') { const n=document.createElement(tag); n.textContent=text; n.className=className; return n; }
function report(run) {
  try {
    const value=JSON.parse(run.result);
    if (Array.isArray(value.tests)) return `${value.summary}\n\nФайлы: ${value.changed_files.join(', ') || 'без изменений'}\nТесты: ${value.tests_passed ? 'пройдены' : 'результат не подтверждён'}\n` + value.tests.map(t=>`${(t.command || []).join(' ')}: ${t.status}\n${t.output || t.reason || ''}`).join('\n') + (value.review_required ? '\nPatch ожидает вашего ревью. Основной проект не изменён.' : '');
  } catch {}
  return run.result || run.error || 'Агент ещё работает.';
}
export function renderExecution(root, detail, {download,rate}) {
  root.replaceChildren();
  const total=detail.runs.reduce((n,r)=>n+Number(r.charged_cost ?? r.reserved_cost ?? 0),0);
  root.append(node('p',`Расходы и активные резервы: ${money(total)}`,'field-note'));
  if (detail.stages?.length) {
    const list=document.createElement('ol'); list.className='execution-stages';
    const lastRun=detail.runs.at(-1)?.id;
    const latest=new Map(); for(const stage of detail.stages.filter(x=>x.run_id===lastRun)) latest.set(stage.name,stage);
    for(const stage of latest.values()) list.append(node('li',`${stages[stage.name] || stage.name} · ${statuses[stage.status] || stage.status}`));
    root.append(list);
  }
  for(const artifact of detail.artifacts || []) {
    const button=node('button',artifact.kind==='patch' ? 'Скачать patch' : 'Скачать отчёт','button secondary');button.type='button';button.onclick=()=>download(artifact.id);root.append(button);
  }
  for(const run of [...detail.runs].reverse()) {
    const box=document.createElement('details');
    box.append(node('summary',`${statuses[run.status] || run.status} · ${new Date(run.started_at).toLocaleString('ru-RU')}`));
    box.append(node('p',`${run.model || 'Локально'} · ${money(run.charged_cost ?? run.reserved_cost)} · токены ${run.input_tokens ?? '—'} / ${run.output_tokens ?? '—'}`,'field-note'));
    const reasons=run.decision?.routing?.reasons;
    if(reasons?.length) { const explanation=document.createElement('details'); explanation.append(node('summary','Почему выбрана эта модель'),node('p',reasons.join('\n'),'routing-reasons'));box.append(explanation); }
    box.append(node('pre',report(run)));
    if(run.model && ['done','yielded'].includes(run.status)) {
      const ratings=node('div','Оценка результата: ','rating-actions');
      for(const [label,value] of [['Полезно',1],['Частично',0.5],['Не помогло',0]]) {const button=node('button',label,'button secondary');button.type='button';button.onclick=()=>rate(run.id,value);ratings.append(button);}
      if(run.quality_score!==null) ratings.append(node('span',String(run.quality_score),'field-note'));box.append(ratings);
    }
    root.append(box);
  }
  const history=document.createElement('details');history.append(node('summary','История задачи'));
  const labels={task_created:'Создана',task_updated:'Изменена',task_control:'Изменён статус',task_cancel_requested:'Запрошена отмена',task_retried:'Создан повторный запуск',run_finished:'Запуск завершён',run_claimed:'Запуск начат',task_waiting:'Ожидание',run_stage:'Этап выполнения'};
  const list=document.createElement('ol');list.className='execution-history';
  for(const event of detail.events || []) {
    const changes=event.metadata?.changes || {};
    const text=Object.entries(changes).map(([key,value])=>`${key}: ${value.before ?? '—'} → ${value.after ?? '—'}`).join('; ');
    list.append(node('li',`${new Date(event.created_at).toLocaleString('ru-RU')} · ${labels[event.kind] || event.kind}${text ? '\n'+text : ''}`));
  }
  history.append(list);root.append(history);
}
