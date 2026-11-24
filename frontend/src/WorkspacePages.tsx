import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ArrowLeft, ArrowRight, Check, CheckCircle2, ChevronRight, ClipboardList, Clock3, FileUp, Inbox, Loader2, Mail, MessageSquare, Paperclip, Plus, RefreshCw, Search, ShieldCheck, Sparkles, Wrench, X } from 'lucide-react';
import { api, upload, humanDate, errorMessage } from './api';
import './workspace.css';
import InboxPage from './InboxPage';
import Escalations from './Escalations';

type Item = Record<string, any>;
type Resource = { data: Item[]; loading: boolean; error: string; reload: () => void };

function rows(value: any): Item[] {
  if (Array.isArray(value)) return value;
  return value?.items || value?.results || value?.data || [];
}

function useRows(path: string): Resource {
  const [data, setData] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    api<any>(path).then(value => { if (active) setData(rows(value)); })
      .catch(err => { if (active) setError(errorMessage(err)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [path, version]);
  return { data, loading, error, reload: () => setVersion(v => v + 1) };
}

const STATUS: Record<string, string> = {
  NEW: 'Новое', OPEN: 'В работе', IN_PROGRESS: 'В работе', WAITING_CUSTOMER: 'Ожидает клиента',
  WAITING_INTERNAL: 'Ожидает отдел', RESOLVED: 'Решено', CLOSED: 'Закрыто', DRAFT: 'Черновик',
  PENDING: 'На согласовании', PENDING_APPROVAL: 'На согласовании', APPROVED: 'Согласовано', REJECTED: 'Отклонено',
  VERIFIED: 'Проверено', CANDIDATE: 'Кандидат', DONE: 'Выполнено', TODO: 'Запланировано', WAITING: 'Ожидает ответа', CANCELLED: 'Отменено', NEEDS_REVIEW: 'Нужна проверка', ARCHIVED: 'В архиве',
};

function Status({ value }: { value?: string }) {
  const v = (value || 'NEW').toUpperCase();
  return <span className={`badge ${['RESOLVED', 'CLOSED', 'VERIFIED', 'APPROVED', 'DONE'].includes(v) ? 'green' : ['WAITING', 'WAITING_CUSTOMER', 'WAITING_INTERNAL', 'PENDING', 'PENDING_APPROVAL', 'CANDIDATE', 'NEEDS_REVIEW'].includes(v) ? 'amber' : 'muted'}`}>{STATUS[v] || v}</span>;
}

function ResourceState({ resource, children, empty, icon }: { resource: Resource; children: ReactNode; empty: string; icon?: ReactNode }) {
  if (resource.loading && !resource.data.length) return <div className="ws-state" role="status"><Loader2 className="spin" size={23} /><span>Загружаем данные…</span></div>;
  if (resource.error) return <div className="ws-state"><div className="error-banner" role="alert">{resource.error}</div><button className="button secondary" onClick={resource.reload}><RefreshCw size={15} />Повторить</button></div>;
  if (!resource.data.length) return <div className="ws-state">{icon || <Inbox size={30} />}<h3>{empty}</h3><p>Здесь появятся реальные данные вашей команды.</p></div>;
  return <>{children}</>;
}

function Notice({ error, message }: { error?: string; message?: string }) {
  return <>{error && <div className="error-banner ws-notice" role="alert">{error}</div>}{message && <div className="ws-success ws-notice" role="status"><CheckCircle2 size={16} />{message}</div>}</>;
}

function PageHeading({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: ReactNode }) {
  return <header className="page-header ws-heading"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div><div className="ws-heading-actions">{children}</div></header>;
}

function CasesPage() {
  const resource = useRows('/cases');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Item | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [note, setNote] = useState('');
  const [serviceNumber, setServiceNumber] = useState('');
  const [task, setTask] = useState('');
  const [assignee, setAssignee] = useState('');
  const [filter, setFilter] = useState('');
  const selected = detail && detail.id === selectedId ? detail : resource.data.find(c => c.id === selectedId);
  const filtered = resource.data.filter(c => `${c.service_number || c.id} ${c.title} ${c.company || ''} ${c.equipment_model || ''}`.toLowerCase().includes(filter.toLowerCase()));

  async function loadDetail(id: number) {
    setError('');
    try { const value = await api<Item>(`/cases/${id}`); setDetail(value); setServiceNumber(value.service_number || ''); }
    catch (err) { setError(errorMessage(err)); }
  }
  useEffect(() => { if (selectedId !== null) { setDetail(null); void loadDetail(selectedId); } }, [selectedId]);
  async function mutate(work: () => Promise<unknown>, success: string) {
    setBusy(true); setError(''); setMessage('');
    try { await work(); resource.reload(); if (selectedId !== null) await loadDetail(selectedId); setMessage(success); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }
  async function createCase(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const { description, ...values } = Object.fromEntries(new FormData(e.currentTarget));
    await mutate(async () => { const created = await api<Item>('/cases', { method: 'POST', body: JSON.stringify(values) }); setCreating(false); setSelectedId(created.id); if (description) await api(`/cases/${created.id}/messages`, { method: 'POST', body: JSON.stringify({ body: description, kind: 'internal' }) }); }, 'Обращение создано');
  }

  return <div className="page-content workspace-page">
    <PageHeading eyebrow="СЕРВИСНЫЙ ЦЕНТР" title="Сервисные обращения" description="Заявки, принятые сервисом. Обычная и внутренняя почтовая переписка остаётся во «Входящих».">
      <button className="button primary" onClick={() => setCreating(v => !v)}>{creating ? <X size={16} /> : <Plus size={16} />}{creating ? 'Закрыть' : 'Новое обращение'}</button>
    </PageHeading>
    <Notice error={error} message={message} />
    {creating && <form className="card ws-create-form" onSubmit={createCase}><div className="panel-header"><h2>Новое обращение</h2><span className="muted">Номер СО назначает оператор</span></div><div className="form-grid">
      <label className="field ws-span-2">Тема обращения<input name="title" required maxLength={240} placeholder="Например, настройка формирователя РЕ38" /></label>
      <label className="field">Клиент / организация<input name="company" required maxLength={240} placeholder="Название компании" /></label>
      <label className="field">Электронная почта<input name="email" type="email" placeholder="service@company.ru" /></label>
      <label className="field">Модель оборудования<input name="equipment_model" placeholder="РЕ38-12" /></label>
      <label className="field">Серийный номер<input name="serial_number" placeholder="Если известен" /></label>
      <label className="field ws-span-2">Описание<textarea name="description" rows={3} required placeholder="Что произошло, когда и при каких условиях?" /></label>
    </div><div className="ws-form-footer"><button type="button" className="button secondary" onClick={() => setCreating(false)}>Отмена</button><button className="button primary" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}Создать обращение</button></div></form>}
    <div className="ws-split">
      <section className="card ws-list-panel"><div className="ws-list-toolbar"><div className="ws-search"><Search size={16} /><input value={filter} onChange={e => setFilter(e.target.value)} aria-label="Поиск обращений" placeholder="Номер, клиент или тема" /></div><span className="ws-count">{resource.data.length}</span></div>
        <ResourceState resource={resource} empty="Пока нет обращений" icon={<ClipboardList size={30} />}><div className="ws-select-list">{filtered.map(item => <button key={item.id} className={`ws-select-row ${item.id === selectedId ? 'selected' : ''}`} onClick={() => { setSelectedId(item.id); setMessage(''); }}><div className="ws-row-top"><span className="ws-record-number">{item.service_number || `Обращение #${item.id}`}</span><Status value={item.status} /></div><strong>{item.title}</strong><span>{item.company || 'Клиент не указан'}</span><div className="ws-row-bottom"><span>{item.equipment_model || 'Модель не указана'}</span><time>{humanDate(item.created_at)}</time></div></button>)}{!filtered.length && <div className="ws-small-empty">Ничего не найдено. Попробуйте другой запрос.</div>}</div></ResourceState>
      </section>
      <section className="card ws-detail-panel">{!selected ? <div className="ws-state"><ClipboardList size={32} /><h3>Вся история — в одном месте</h3><p>Выберите обращение, чтобы посмотреть детали,<br />добавить заметку или поручение.</p></div> : <>
        <div className="ws-detail-header"><div><span className="eyebrow">{selected.service_number || `Обращение #${selected.id}`}</span><h2>{selected.title}</h2></div><Status value={selected.status} /></div>
        <div className="ws-meta-grid"><div><span>Клиент</span><strong>{selected.company || 'Не указан'}</strong></div><div><span>Электронная почта</span><strong>{selected.email || 'Не указана'}</strong></div><div><span>Оборудование</span><strong>{selected.equipment_model || 'Не указано'}</strong></div><div><span>Серийный номер</span><strong>{selected.serial_number || 'Не указан'}</strong></div></div>
        <form className="ws-service-number-form" onSubmit={e => { e.preventDefault(); void mutate(() => api(`/cases/${selected.id}`, { method: 'PATCH', body: JSON.stringify({ service_number: serviceNumber.trim() || null }) }), 'Номер СО сохранён'); }}><label className="field">Номер СО<input value={serviceNumber} onChange={e => setServiceNumber(e.target.value)} placeholder="Назначается оператором вручную" maxLength={80}/></label><button className="button secondary" disabled={busy || serviceNumber.trim() === (selected.service_number || '')}>Сохранить номер</button></form>
        {selected.description && <p className="ws-description">{selected.description}</p>}
        <div className="ws-status-control"><label htmlFor="case-status">Статус обращения</label><select id="case-status" value={selected.status} disabled={busy} onChange={e => { const status = e.target.value; void mutate(() => api(`/cases/${selected.id}`, { method: 'PATCH', body: JSON.stringify({ status }) }), 'Статус обновлён'); }}>{['new', 'in_progress', 'waiting', 'resolved'].map(s => <option value={s} key={s}>{STATUS[s.toUpperCase()]}</option>)}</select></div>
        <div className="ws-section"><h3><MessageSquare size={17} />История и заметки</h3>{selected.messages?.length ? <div className="ws-timeline">{selected.messages.map((m: Item) => <article key={m.id}><div><strong>{m.kind === 'internal' ? 'Внутренняя заметка' : m.sender || 'Сообщение'}</strong><time>{humanDate(m.created_at)}</time></div><p>{m.body || m.content}</p></article>)}</div> : <p className="muted ws-hint">Внутренние заметки видны только сотрудникам.</p>}
          <form onSubmit={e => { e.preventDefault(); void mutate(async () => { await api(`/cases/${selected.id}/messages`, { method: 'POST', body: JSON.stringify({ body: note, kind: 'internal' }) }); setNote(''); }, 'Заметка добавлена'); }}><label className="field"><textarea value={note} onChange={e => setNote(e.target.value)} required rows={3} placeholder="Добавьте контекст для команды…" aria-label="Внутренняя заметка" /></label><div className="ws-form-footer"><button className="button secondary" disabled={busy || !note.trim()}><Plus size={15} />Добавить заметку</button></div></form>
        </div>
        <div className="ws-section"><h3><ClipboardList size={17} />Внутренние поручения</h3><div className="ws-task-list">{selected.tasks?.map((t: Item) => <div className="ws-task" key={t.id}><button className={`ws-task-check ${t.status === 'done' ? 'done' : ''}`} aria-label={t.status === 'done' ? 'Открыть поручение повторно' : 'Завершить поручение'} disabled={busy} onClick={() => void mutate(() => api(`/tasks/${t.id}`, { method: 'PATCH', body: JSON.stringify({ status: t.status === 'done' ? 'open' : 'done' }) }), 'Поручение обновлено')}>{t.status === 'done' && <Check size={13} />}</button><div><strong className={t.status === 'done' ? 'ws-completed' : ''}>{t.title}</strong><span>{t.assignee || t.department || 'Без ответственного'}</span></div><Status value={t.status} /></div>)}</div><form onSubmit={e => { e.preventDefault(); void mutate(async () => { await api(`/cases/${selected.id}/tasks`, { method: 'POST', body: JSON.stringify({ title: task, department: assignee }) }); setTask(''); setAssignee(''); }, 'Поручение создано'); }} className="ws-task-form"><label className="field"><input value={task} onChange={e => setTask(e.target.value)} required placeholder="Что нужно сделать?" aria-label="Новое поручение" /></label><label className="field"><input value={assignee} onChange={e => setAssignee(e.target.value)} required placeholder="Отдел-исполнитель" aria-label="Отдел-исполнитель" /></label><button className="button secondary" disabled={busy || !task.trim() || !assignee.trim()} aria-label="Создать поручение"><Plus size={17} /></button></form></div>
      </>}</section>
    </div>
  </div>;
}


function ApprovalsPage() {
  const resource = useRows('/approvals');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [filter, setFilter] = useState('PENDING');
  const selected = resource.data.find(x => x.id === selectedId);
  const filtered = resource.data.filter(d => filter === 'ALL' || (filter === 'PENDING' ? ['PENDING', 'PENDING_APPROVAL', 'DRAFT'].includes(d.status) : d.status === filter));
  async function update(status?: string) {
    if (!selected) return;
    setBusy(true); setError(''); setMessage('');
    try { await api(`/approvals/${selected.id}`, { method: 'PATCH', body: JSON.stringify({ body, ...(status ? { status } : {}) }) }); resource.reload(); setMessage(status === 'APPROVED' ? 'Черновик согласован. Отправка не выполнялась.' : status === 'REJECTED' ? 'Черновик отклонён.' : 'Изменения сохранены.'); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }
  return <div className="page-content workspace-page"><PageHeading eyebrow="КОНТРОЛЬ КАЧЕСТВА" title="Согласование ответов" description="Последнее слово всегда за сотрудником." /><Notice error={error} message={message} /><div className="ws-safe-strip"><ShieldCheck size={16} /><span>Согласование сохраняет решение в системе. Реальная отправка почты отключена.</span></div><div className="ws-split"><section className="card ws-list-panel"><div className="ws-list-toolbar"><select aria-label="Фильтр согласований" value={filter} onChange={e => setFilter(e.target.value)}><option value="PENDING">Ожидают проверки</option><option value="APPROVED">Согласованы</option><option value="REJECTED">Отклонены</option><option value="ALL">Все черновики</option></select><span className="ws-count">{filtered.length}</span></div><ResourceState resource={resource} empty="Все ответы под контролем" icon={<ShieldCheck size={30} />}><div className="ws-select-list">{filtered.map(d => <button className={`ws-select-row ${d.id === selectedId ? 'selected' : ''}`} key={d.id} onClick={() => { setSelectedId(d.id); setBody(d.body || ''); setMessage(''); }}><div className="ws-row-top"><span className="ws-record-number">Черновик #{d.id}</span><Status value={d.status} /></div><strong>{d.subject || 'Ответ клиенту'}</strong><span>{d.recipient || 'Получатель не указан'}</span><div className="ws-row-bottom"><span>Требует внимания сотрудника</span><time>{humanDate(d.created_at)}</time></div></button>)}{!filtered.length && <div className="ws-state"><CheckCircle2 size={28} /><h3>В этой очереди всё готово</h3><p>Выберите другой статус, чтобы посмотреть историю.</p></div>}</div></ResourceState></section><section className="card ws-approval-detail">{selected ? <><div className="ws-detail-header"><div><span className="eyebrow">ПРОВЕРКА ОТВЕТА</span><h2>{selected.subject || 'Ответ клиенту'}</h2><p className="muted">Кому: {selected.recipient}</p></div><Status value={selected.status} /></div>{selected.needs_review&&<div className="notice-banner" role="alert">{selected.warning||'Этот старый черновик требует повторной проверки содержания и получателя.'}</div>}<label className="field">Текст ответа<textarea rows={15} value={body} onChange={e => setBody(e.target.value)} disabled={busy || ['APPROVED', 'REJECTED'].includes(selected.status)} /></label><div className="ws-approval-actions"><button className="button secondary" disabled={busy || !body.trim() || ['APPROVED', 'REJECTED'].includes(selected.status)} onClick={() => void update()}>Сохранить правки</button><div><button className="button ws-danger-button" disabled={busy || ['APPROVED', 'REJECTED'].includes(selected.status)} onClick={() => void update('REJECTED')}><X size={15} />Отклонить</button><button className="button primary" disabled={busy || !body.trim() || ['APPROVED', 'REJECTED'].includes(selected.status)} onClick={() => void update('APPROVED')}><Check size={16} />Согласовать</button></div></div><p className="ws-hint muted">Проверьте технические данные, адресата и полноту ответа перед согласованием.</p></> : <div className="ws-state"><ShieldCheck size={34} /><h3>Проверка перед следующим шагом</h3><p>Выберите черновик, внесите правки<br />и сохраните своё решение.</p></div>}</section></div></div>;
}

function EquipmentPage() {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const resource = useRows(`/equipment${search ? `?q=${encodeURIComponent(search)}` : ''}`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [report,setReport]=useState<Item|null>(null);
  const input = useRef<HTMLInputElement>(null);
  async function importFile(file?: File) {
    if (!file) return;
    setBusy(true); setError(''); setMessage('');
    try { const result = await upload<Item>('/equipment/import', [file], 'file'); setReport(result); resource.reload(); setMessage(`Реестр обработан${result.imported !== undefined ? `. Добавлено записей: ${result.imported}` : ''}${result.enriched ? `, дополнено: ${result.enriched}` : ''}${result.duplicates ? `, повторов: ${result.duplicates}` : ''}${result.skipped ? `, пропущено: ${result.skipped}` : ''}${result.ambiguous ? `, конфликтов: ${result.ambiguous} (исходные записи сохранены)` : ''}.`); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); if (input.current) input.current.value = ''; }
  }
  return <div className="page-content workspace-page"><PageHeading eyebrow="ОБОРУДОВАНИЕ ПОСТЭКС" title="Реестр оборудования" description="Серийные номера и данные машин для точной диагностики."><input hidden ref={input} type="file" accept=".xlsx" onChange={e => void importFile(e.target.files?.[0])} /><button className="button primary" onClick={() => input.current?.click()} disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <FileUp size={16} />}Импорт Excel</button></PageHeading><Notice error={error} message={message} />{report&&report.issues?.length>0&&<details className="card ws-import-report"><summary>Строки, требующие внимания · {report.issues.length}</summary><div className="ws-table-scroll"><table className="data-table"><thead><tr><th>Строка</th><th>Серийный номер</th><th>Причина</th></tr></thead><tbody>{report.issues.map((issue:Item,index:number)=><tr key={index}><td>{issue.row}</td><td>{issue.serial||'—'}</td><td>{issue.reason?.startsWith('serial_conflict')?'Данные отличаются от имеющейся записи. Исходная запись сохранена.':issue.reason==='invalid_serial'?'Некорректный серийный номер':issue.reason==='empty_normalized_serial'?'Пустой серийный номер':issue.reason}</td></tr>)}</tbody></table></div></details>}<section className="card ws-equipment-card"><div className="ws-table-toolbar"><form className="ws-equipment-search" onSubmit={e => { e.preventDefault(); setSearch(query); }}><div className="ws-search"><Search size={17} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Серийный номер, модель или клиент" aria-label="Поиск оборудования" /></div><button className="button secondary">Найти</button>{search && <button className="icon-button" type="button" aria-label="Сбросить поиск" onClick={() => { setQuery(''); setSearch(''); }}><X size={17} /></button>}</form><span className="muted">Записей: {resource.data.length}</span></div><ResourceState resource={resource} empty={search ? 'Оборудование не найдено' : 'Добавьте реестр оборудования'} icon={<Wrench size={30} />}><div className="ws-table-scroll"><table className="data-table ws-equipment-table"><thead><tr><th>Серийный номер</th><th>Модель оборудования</th><th>Клиент</th><th>Дата отгрузки</th><th>Проект / наименование</th></tr></thead><tbody>{resource.data.map(item => <tr key={item.id}><td><span className="ws-serial"><Wrench size={15} />{item.serial_number || '—'}</span></td><td><strong>{item.model || item.equipment_model || '—'}</strong></td><td>{item.customer || item.customer_name || '—'}</td><td>{item.shipped_at || '—'}</td><td className="ws-note-cell">{[item.project, item.name].filter(Boolean).join(' · ') || '—'}</td></tr>)}</tbody></table></div></ResourceState></section><div className="ws-help-note"><FileUp size={18} /><div><strong>Работайте с исходным реестром</strong><p>Загрузите файл .xlsx с серийными номерами. Результат импорта и ошибки проверки появятся здесь.</p></div></div></div>;
}

function SolutionsPage() {
  const resource = useRows('/solutions');
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState('ALL');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [evidence, setEvidence] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const filtered = resource.data.filter(x => filter === 'ALL' || x.status === filter);
  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = e.currentTarget;
    const value = { ...Object.fromEntries(new FormData(form)), customer_confirmed: false };
    setBusy(true); setError(''); setMessage('');
    try { await api('/solutions', { method: 'POST', body: JSON.stringify(value) }); setCreating(false); resource.reload(); setMessage('Кандидат добавлен. Подтвердите результат, чтобы включить его в проверенные решения.'); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }
  async function verify(e: FormEvent) {
    e.preventDefault(); if (!confirmed || !confirmId) return;
    setBusy(true); setError(''); setMessage('');
    try { await api(`/solutions/${confirmId}`, { method: 'PATCH', body: JSON.stringify({ status: 'VERIFIED', customer_confirmed: true, source: [resource.data.find(s => s.id === confirmId)?.source, `Подтверждение результата: ${evidence}`].filter(Boolean).join('\n') }) }); resource.reload(); setConfirmId(null); setEvidence(''); setConfirmed(false); setMessage('Решение проверено и сохранено в базе опыта.'); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }
  async function setVisibility(solution:Item){const visibility=solution.visibility==='public'?'internal':'public';if(!window.confirm(visibility==='public'?`Открыть решение «${solution.title}» посетителям сайта? Убедитесь, что оно не содержит данных клиента и проверено сотрудником.`:`Ограничить решение «${solution.title}» только сотрудниками?`))return;setBusy(true);setError('');try{await api(`/solutions/${solution.id}`,{method:'PATCH',body:JSON.stringify({visibility,status:'VERIFIED'})});resource.reload();setMessage(visibility==='public'?'Решение опубликовано для клиентского помощника.':'Решение доступно только сотрудникам.');}catch(err){setError(errorMessage(err));}finally{setBusy(false);}}
  return <div className="page-content workspace-page"><PageHeading eyebrow="ОПЫТ СЕРВИСНОЙ КОМАНДЫ" title="Проверенные решения" description="Сохраняйте опыт, который действительно помог клиентам."><button className="button primary" onClick={() => setCreating(v => !v)}>{creating ? <X size={16} /> : <Plus size={16} />}{creating ? 'Закрыть' : 'Добавить решение'}</button></PageHeading><Notice error={error} message={message} />{creating && <form className="card ws-create-form" onSubmit={create}><div className="panel-header"><h2>Кандидат в базу решений</h2><Status value="CANDIDATE" /></div><div className="form-grid"><label className="field ws-span-2">Название<input name="title" required placeholder="Коротко опишите результат" /></label><label className="field">Модель оборудования<input name="equipment_model" required placeholder="Точная модель" /></label><label className="field">Источник / номер обращения<input name="source" required placeholder="Номер обращения или документ" /></label><label className="field ws-span-2">Симптомы<textarea name="symptoms" rows={2} required placeholder="Как проявлялась проблема?" /></label><label className="field ws-span-2">Действия и результат<textarea name="actions" rows={3} required placeholder="Что сделали и как проверили результат?" /></label><label className="field ws-span-2">Ограничения<textarea name="limitations" rows={2} placeholder="Для каких условий решение проверено?"/></label></div><div className="ws-form-footer"><button type="button" className="button secondary" onClick={() => setCreating(false)}>Отмена</button><button className="button primary" disabled={busy}>Сохранить кандидата</button></div></form>}<div className="ws-tabs" role="group" aria-label="Статус решений">{[['ALL', 'Все решения'], ['VERIFIED', 'Проверенные'], ['CANDIDATE', 'Кандидаты']].map(([value, title]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{title}<span>{resource.data.filter(x => value === 'ALL' || x.status === value).length}</span></button>)}</div><ResourceState resource={resource} empty="Ваш опыт станет базой знаний" icon={<CheckCircle2 size={32} />}><div className="ws-solutions-grid">{filtered.map(s => <article className="card ws-solution" key={s.id}><div className="ws-row-top"><span className="ws-model-tag"><Wrench size={13} />{s.equipment_model || 'Оборудование'}</span><Status value={s.status} /></div><h2>{s.title}</h2><div className="ws-solution-section"><span>Симптомы</span><p>{s.symptoms}</p></div><div className="ws-solution-section"><span>Что помогло</span><p>{s.actions}</p></div>{s.limitations&&<div className="ws-solution-section"><span>Ограничения</span><p>{s.limitations}</p></div>}<div className="ws-solution-source"><ClipboardList size={14} /><span>{s.source || 'Источник не указан'}</span></div>{s.status !== 'VERIFIED' && <button className="button secondary" onClick={() => { setConfirmId(confirmId === s.id ? null : s.id); setEvidence(''); setConfirmed(false); }}><ShieldCheck size={15} />Проверить решение</button>}{confirmId === s.id && <form className="ws-verification-form" onSubmit={verify}><label className="field">Подтверждение результата<textarea value={evidence} onChange={e => setEvidence(e.target.value)} required rows={3} placeholder="Кто, когда и как подтвердил, что проблема решена?" /></label><label className="ws-checkbox"><input type="checkbox" checked={confirmed} required onChange={e => setConfirmed(e.target.checked)} /><span>Клиент подтвердил результат, данные проверены</span></label><button className="button primary" disabled={busy || !confirmed || !evidence.trim()}><Check size={15} />Подтвердить решение</button></form>}{s.status === 'VERIFIED' && <><div className="ws-verified"><CheckCircle2 size={15} /><span>{s.evidence || 'Результат подтверждён клиентом'}</span></div><div className="ws-solution-publication"><span className="badge muted">{s.visibility==='public'?'Доступно посетителям':'Только сотрудники'}</span><button className="button secondary small" disabled={busy} onClick={()=>void setVisibility(s)}>{s.visibility==='public'?'Сделать внутренним':'Открыть клиентам'}</button></div></>}</article>)}{!filtered.length && <div className="ws-state"><CheckCircle2 size={28} /><h3>Решений с таким статусом пока нет</h3><p>Добавьте результат сервисного обращения.</p></div>}</div></ResourceState></div>;
}

const AUDIT_LABELS: Record<string, string> = { 'case.created': 'Создано обращение', 'case.updated': 'Обновлено обращение', 'task.created': 'Создано поручение', 'task.updated': 'Обновлено поручение', 'draft.created': 'Создан черновик', 'draft.updated': 'Обновлён черновик', 'mail.imported': 'Импортировано письмо', 'solution.created': 'Добавлено решение', 'solution.updated': 'Обновлено решение', 'equipment.imported': 'Импорт оборудования', 'login': 'Вход в систему' };

function AuditPage() {
  const resource = useRows('/audit');
  const [query, setQuery] = useState('');
  const filtered = resource.data.filter(row => `${row.action || ''} ${row.actor || row.username || ''} ${row.entity_type || row.entity || ''}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="page-content workspace-page"><PageHeading eyebrow="БЕЗОПАСНОСТЬ И КОНТРОЛЬ" title="Журнал действий" description="Прозрачная история изменений в рабочем пространстве."><button className="button secondary" onClick={resource.reload} disabled={resource.loading}><RefreshCw size={16} className={resource.loading ? 'spin' : ''} />Обновить</button></PageHeading><section className="card ws-equipment-card"><div className="ws-table-toolbar"><div className="ws-search"><Search size={17} /><input placeholder="Действие или сотрудник" aria-label="Фильтр журнала" value={query} onChange={e => setQuery(e.target.value)} /></div><span className="muted">Событий: {filtered.length}</span></div><ResourceState resource={resource} empty="История действий пока пуста" icon={<Clock3 size={30} />}><div className="ws-table-scroll"><table className="data-table ws-audit-table"><thead><tr><th>Дата и время</th><th>Сотрудник</th><th>Действие</th><th>Объект</th></tr></thead><tbody>{filtered.map(row => <tr key={row.id}><td><span className="ws-audit-time"><Clock3 size={14} />{humanDate(row.created_at)}</span></td><td><span className="ws-actor-avatar">{String(row.actor || row.username || 'С').slice(0, 1).toUpperCase()}</span>{row.actor || row.username || (row.user_id ? `Сотрудник #${row.user_id}` : 'Система')}</td><td>{AUDIT_LABELS[row.action] || row.action}</td><td><span className="ws-record-number">{row.entity_type || row.entity || '—'}{row.entity_id ? ` #${row.entity_id}` : ''}</span></td></tr>)}</tbody></table>{!filtered.length && <div className="ws-small-empty">События не найдены</div>}</div></ResourceState></section><div className="ws-help-note"><ShieldCheck size={18} /><div><strong>История сохраняется автоматически</strong><p>Создание, изменение и согласование фиксируются с датой и автором действия.</p></div></div></div>;
}

export function WorkspacePage({ page, onNavigate }: { page: string; onNavigate: (path: string) => void }) {
  switch (page.replace(/^.*\//, '')) {
    case 'cases': return <CasesPage />;
    case 'escalations': return <Escalations onNavigate={onNavigate}/>;
    case 'inbox': return <InboxPage onNavigate={onNavigate} />;
    case 'approvals': return <ApprovalsPage />;
    case 'equipment': return <EquipmentPage />;
    case 'solutions': return <SolutionsPage />;
    case 'audit': return <AuditPage />;
    default: return <div className="page-content"><div className="ws-state"><ArrowLeft size={28} /><h2>Раздел не найден</h2><button className="button secondary" onClick={() => onNavigate('/admin/inbox')}>К входящим</button></div></div>;
  }
}
