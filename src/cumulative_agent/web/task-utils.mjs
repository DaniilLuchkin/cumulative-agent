export const statusLabels = {inbox:'Напоминание',ready:'В очереди',running:'В работе',blocked:'Ожидает',done:'Выполнена',failed:'Ошибка',cancelled:'Отменена'};
export function priorityLabel(value) {
  return ({0:'Низкий',10:'Обычный',20:'Высокий',30:'Срочный'})[value] ?? `Приоритет ${value}`;
}
export function priorityTone(value) { return value >= 30 ? 'urgent' : value >= 20 ? 'high' : value >= 10 ? 'normal' : 'low'; }
export function toLocalInput(value) {
  if (!value) return '';
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return '';
  const pad = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function deadlineForSave(value, original = null) {
  if (!value) return null;
  if (original && value === toLocalInput(original)) return original;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime()) || toLocalInput(date) !== value) throw new Error('Выберите корректную дату и время в вашем часовом поясе.');
  return date.toISOString();
}
export function dayBounds(date = new Date()) {
  const start = new Date(date); start.setHours(0,0,0,0);
  const end = new Date(start); end.setDate(end.getDate()+1);
  return {day_start:start.toISOString(),day_end:end.toISOString()};
}
export function deadlineLabel(value, now = new Date()) {
  if (!value) return 'Без срока';
  const date = new Date(value), today = new Date(now); today.setHours(0,0,0,0);
  const tomorrow = new Date(today); tomorrow.setDate(tomorrow.getDate()+1);
  const time = date.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  if (date.toDateString() === today.toDateString()) return `Сегодня, ${time}`;
  if (date.toDateString() === tomorrow.toDateString()) return `Завтра, ${time}`;
  return `${date.toLocaleDateString('ru-RU',{day:'numeric',month:'short',...(date.getFullYear() !== now.getFullYear() ? {year:'numeric'} : {})})}, ${time}`;
}
export function canComplete(task) { return !task.proposal_action && !['running','cancelled'].includes(task.status) && (task.status !== 'done' || task.completed_manually); }
export function listQuery({view,search,project,sort,offset}, date = new Date()) {
  return new URLSearchParams({...dayBounds(date),status:['done','cancelled'].includes(view) ? view : 'active',kind:view === 'background' ? 'background' : 'user',due:['today','overdue'].includes(view) ? view : 'all',search,project,sort,offset:String(offset),limit:'50'});
}
