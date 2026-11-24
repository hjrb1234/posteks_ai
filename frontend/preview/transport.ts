/** Isolated browser-only test transport for the downloadable design preview.
 * Contains no company documents, real customer data, server credentials or AI calls.
 * Never import this module from the production entry.
 */
type Row = Record<string, any>;
const stamp = () => new Date().toISOString();
const user = {id: 1, username: 'Тестовый просмотр', role: 'admin'};
const text = 'ТЕСТОВЫЙ ДОКУМЕНТ ДЛЯ ПРОСМОТРА ДИЗАЙНА. Это искусственная запись. Инструкции компании сюда не включены. Технические рекомендации по этому тексту выдавать нельзя.';
let authenticated = true;
let counter = 100;
const id = () => ++counter;
const documents: Row[] = ['PE38-12', 'PE38-17', 'AZ01'].map((model, i) => ({
  id: i + 1, title: `TEST · ${model} · пример документа`, filename: `TEST-${model}.txt`,
  models: [model], category: 'Тестовые материалы', visibility: 'internal', status: 'indexed',
  pages: 1, chunks: 1, size_bytes: new TextEncoder().encode(text).length, created_at: stamp(),
  version: 1, searchable: true, _text: `${text}\nОбозначение тестовой модели: ${model}.`,
}));
const cases: Row[] = [{id: 1, title: 'TEST · Проверка интерфейса обращения', company: 'Тестовая организация',
  email: 'customer@example.com', contact: 'Тестовый контакт', equipment_model: 'PE38-12', serial_number: 'TEST-0001',
  service_number: null, status: 'new', priority: 'normal', source: 'TEST_PREVIEW',
  created_at: stamp(), updated_at: stamp(), messages: [], tasks: []}];
const inbox: Row[] = [{id: 1, case_id: 1, subject: 'TEST · Вопрос по оборудованию',
  sender: 'customer@example.com', sender_name: 'Тестовый клиент', recipients: ['service@example.com'],
  message_id: '<preview@example.com>', body: 'ТЕСТОВОЕ ПИСЬМО. Это вымышленная запись для просмотра интерфейса.\n\nПодскажите, какие сведения указать в обращении по оборудованию?',
  attachments: [], source: 'TEST_PREVIEW', created_at: stamp(), sent_at: stamp()}];
const approvals: Row[] = [{id: 1, inbox_id: 1, case_id: 1, subject: 'Re: TEST · Вопрос по оборудованию',
  recipient: 'customer@example.com', body: 'ТЕСТОВЫЙ ЧЕРНОВИК. Пожалуйста, укажите модель, заводской номер и наблюдаемые симптомы. Это текст для просмотра формы, реальной отправки нет.',
  status: 'PENDING', created_at: stamp(), updated_at: stamp()}];
const equipment: Row[] = [{id: 1, serial_number: 'TEST-0001', model: 'PE38-12', project: 'Тестовый проект',
  name: 'Тестовая запись оборудования', shipped_at: null, customer: 'Тестовая организация', warranty_status: 'unknown'}];
const solutions: Row[] = [{id: 1, title: 'TEST · Кандидат в базу опыта', equipment_model: 'PE38-12',
  symptoms: 'Вымышленный пример для проверки карточки.', actions: 'Техническое решение отсутствует. Запись проверяет только интерфейс.',
  source: 'Тестовый сценарий просмотра', customer_confirmed: false, status: 'CANDIDATE', visibility: 'internal', created_at: stamp()}];
const audit: Row[] = [];
const conversations: Row[] = [];
const models: Row = {main: 'gpt-4.1-mini', fast: 'gpt-4.1-mini', vision: 'gpt-4.1-mini', audio: 'gpt-4o-mini-transcribe', embedding: 'text-embedding-3-small'};

function event(action: string, type: string, entity: any) {
  audit.unshift({id: id(), actor: user.username, action, entity_type: type, entity_id: String(entity), created_at: stamp()});
}
const response = (value: any, status = 200) => new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
const error = (detail: string, status = 400) => response({detail}, status);
const publicDoc = (doc: Row) => Object.fromEntries(Object.entries(doc).filter(([key]) => !key.startsWith('_')));
const find = (rows: Row[], key: string | number) => rows.find(item => String(item.id) === String(key));
function sources(query: string, document?: number, model?: string): Row[] {
  const match = documents.filter(d => (!document || d.id === document) && (!model || d.models.includes(model)));
  if (!query.trim()) return [];
  return match.slice(0, 2).map(d => ({document_id: d.id, title: d.title, filename: d.filename,
    page: 1, pages: 1, text: d._text, model: d.models[0] || '', score: 1, source_type: 'document'}));
}
function answer(body: Row): Row {
  const content = 'ТЕСТОВЫЙ ПРОСМОТР ИНТЕРФЕЙСА. Сообщение отображено в диалоге.\n\nЗдесь доступны только искусственные файлы для проверки карточек источников. Реальный поиск по вашим инструкциям и ответы OpenAI работают после запуска Python-сервера. Данные компании в этот файл не включены.';
  return {message: {id: id(), role: 'assistant', content, sources: sources(body.message, Number(body.document_id) || undefined, body.equipment_model || body.model), created_at: stamp()},
    engine: 'preview', action: 'request_information', missing_information: ['Для реальной обработки запустите приложение с backend'],
    trace: [{step: 'preview', status: 'test', detail: 'Тестовые данные в памяти браузера. Внешние действия не выполняются.'}]};
}

async function dispatch(path: string, method: string, payload: any, url: URL): Promise<Response> {
  const segments = path.split('/').filter(Boolean);
  if (path === '/health') return response({status: 'ok', mode: 'TEST', safe_mode: true, mail_send_enabled: false, documents_indexed: documents.length, ai_configured: false});
  if (path === '/auth/me') return response({authenticated, user: authenticated ? user : null});
  if (path === '/auth/login' && method === 'POST') { authenticated = true; return response({authenticated: true, user}); }
  if (path === '/auth/logout') { authenticated = false; return response({ok: true}); }
  if (path === '/settings/check-openai') return response({configured: false, connected: false, error: 'Это автономный просмотр. Подключение OpenAI доступно в запущенном приложении.', models: []});
  if (path === '/settings') {
    if (method === 'PATCH' && payload.models) Object.assign(models, payload.models);
    return response({mode: 'TEST', models, ai_configured: false, mail_send_enabled: false, capabilities: {local_search: false, preview: true}});
  }
  if (path === '/mail/status') return response({configured: false, mode: 'TEST', sending_enabled: false, imap_host: '', username_present: false});
  if (path === '/mail/check') return error('Автономный TEST-просмотр не подключается к почте.', 409);
  if (path === '/mail/import') return error('Для импорта настоящих EML запустите приложение. В автономном просмотре используйте тестовое письмо.', 422);
  if (path === '/equipment/import') return error('Импорт реестра выполняется локальным Python-сервером. Конфиденциальные файлы в просмотр не загружайте.', 422);
  if (path === '/playground' && method === 'POST') return response(answer(payload));
  if (path === '/chat' && method === 'POST') {
    let conversation = find(conversations, payload.conversation_id || '');
    if (!conversation) { conversation = {id: `test-${id()}`, title: payload.message.slice(0, 70), updated_at: stamp(), messages: []}; conversations.unshift(conversation); }
    const result = answer(payload);
    conversation.messages.push({id: id(), role: 'user', content: payload.message, created_at: stamp()}, result.message);
    conversation.updated_at = stamp();
    return response({...result, conversation_id: conversation.id});
  }
  if (segments[0] === 'conversations') {
    if (segments.length === 1) return response(conversations.map(({messages: _messages, ...rest}) => rest));
    const item = find(conversations, segments[1]);
    if (!item) return error('Диалог не найден', 404);
    if (method === 'DELETE') conversations.splice(conversations.indexOf(item), 1);
    return response(method === 'DELETE' ? {ok: true} : item);
  }
  if (path === '/knowledge/search') return response({results: sources(url.searchParams.get('q') || '', Number(url.searchParams.get('document_id')) || undefined, url.searchParams.get('model') || undefined), mode: 'preview'});
  if (segments[0] === 'documents') {
    if (segments.length === 1) {
      if (method === 'POST' && payload instanceof FormData) {
        const added = [];
        for (const entry of payload.getAll('files')) {
          if (!(entry instanceof File) || !/\.(txt|md)$/i.test(entry.name) || entry.size > 1_000_000) return error('В просмотре доступны только TXT/MD до 1 МБ. Для PDF и OCR нужен Python-сервер.', 422);
          const doc: Row = {id: id(), title: `TEST · ${entry.name}`, filename: entry.name, models: [], category: 'Тестовый файл', visibility: 'internal', status: 'indexed', pages: 1, chunks: 1, version: 1, size_bytes: entry.size, created_at: stamp(), _text: await entry.text()};
          documents.unshift(doc); added.push(publicDoc(doc));
        }
        return response(added);
      }
      return response(documents.map(publicDoc));
    }
    const doc = find(documents, segments[1]);
    if (!doc) return error('Документ не найден', 404);
    if (segments[2] === 'pages') return response({document_id: doc.id, title: doc.title, page: 1, total_pages: 1, text: doc._text, image_url: null});
    if (segments[2] === 'file') return new Response(doc._text, {headers: {'Content-Type': 'text/plain;charset=utf-8'}});
    if (segments[2] === 'reindex') return response(publicDoc(doc));
    if (method === 'DELETE') { documents.splice(documents.indexOf(doc), 1); event('document.deleted', 'document', doc.id); return response({ok: true}); }
    if (method === 'PATCH') { Object.assign(doc, payload); event('document.updated', 'document', doc.id); }
    return response(publicDoc(doc));
  }
  if (segments[0] === 'cases') {
    if (segments.length === 1) {
      if (method === 'POST') { const item = {id: id(), ...payload, source: 'TEST_PREVIEW', service_number: null, status: 'new', priority: 'normal', messages: [], tasks: [], created_at: stamp(), updated_at: stamp()}; cases.unshift(item); event('case.created', 'case', item.id); return response(item, 201); }
      return response(cases);
    }
    const item = find(cases, segments[1]);
    if (!item) return error('Обращение не найдено', 404);
    if (segments[2] === 'messages' && method === 'POST') { const note = {id: id(), ...payload, author: user.username, created_at: stamp()}; item.messages.push(note); event('case.message_recorded', 'case', item.id); return response(note, 201); }
    if (segments[2] === 'tasks' && method === 'POST') { const task = {id: id(), ...payload, status: 'open', created_at: stamp()}; item.tasks.push(task); event('task.created', 'task', task.id); return response(task, 201); }
    if (method === 'PATCH') { if (payload.service_number && cases.some(c => c.id !== item.id && c.service_number === payload.service_number)) return error('Номер уже используется', 409); Object.assign(item, payload, {updated_at: stamp()}); event('case.updated', 'case', item.id); }
    return response(item);
  }
  if (segments[0] === 'tasks') {
    const task = cases.flatMap(c => c.tasks).find(t => String(t.id) === segments[1]);
    if (!task) return error('Поручение не найдено', 404);
    Object.assign(task, payload); event('task.updated', 'task', task.id); return response(task);
  }
  if (segments[0] === 'inbox') {
    if (segments.length === 1) return response(inbox);
    const message = find(inbox, segments[1]);
    if (!message) return error('Письмо не найдено', 404);
    if (segments[2] === 'draft') {
      if (!payload.body) return error('AI не подключён в автономном просмотре. Введите тестовый черновик вручную.', 503);
      const draft = {id: id(), case_id: message.case_id, inbox_id: message.id, subject: `Re: ${message.subject}`, recipient: payload.recipient || message.sender, body: payload.body, status: 'PENDING', created_at: stamp()};
      approvals.unshift(draft); event('draft.created', 'approval', draft.id); return response(draft, 201);
    }
    return response(message);
  }
  if (segments[0] === 'approvals') {
    if (segments.length === 1) return response(approvals);
    if (segments[2] === 'send') return error('Отправка запрещена в TEST-просмотре', 409);
    const item = find(approvals, segments[1]);
    if (!item) return error('Черновик не найден', 404);
    Object.assign(item, payload); event('draft.updated', 'approval', item.id); return response(item);
  }
  if (segments[0] === 'solutions') {
    if (segments.length === 1) {
      if (method === 'POST') { const item = {id: id(), ...payload, status: 'CANDIDATE', visibility: 'internal', created_at: stamp()}; solutions.unshift(item); event('solution.created', 'solution', item.id); return response(item, 201); }
      return response(solutions);
    }
    const item = find(solutions, segments[1]); if (!item) return error('Решение не найдено', 404);
    Object.assign(item, payload); event('solution.updated', 'solution', item.id); return response(item);
  }
  if (segments[0] === 'knowledge' && segments[1] === 'solutions') return response(find(solutions, segments[2]) || {});
  if (path === '/equipment') {
    const q = (url.searchParams.get('q') || '').toLowerCase();
    return response(equipment.filter(item => JSON.stringify(item).toLowerCase().includes(q)));
  }
  if (path === '/audit') return response(audit);
  if (path === '/departments') return response([]);
  return error('Эта операция доступна после запуска локального приложения с backend.', 422);
}

export function installPreviewTransport() {
  window.fetch = async (input: RequestInfo | URL, options: RequestInit = {}) => {
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, 'https://posteks-preview.invalid');
    if (!url.pathname.startsWith('/api/')) throw new Error('Внешняя сеть отключена в автономном TEST-просмотре');
    let body: any = options.body instanceof FormData ? options.body : {};
    if (typeof options.body === 'string') { try { body = JSON.parse(options.body); } catch { return error('Неверные данные', 422); } }
    return dispatch(url.pathname.slice(4), options.method || 'GET', body, url);
  };
  document.addEventListener('click', async event => {
    const anchor = (event.target as Element | null)?.closest('a');
    if (!anchor) return;
    const href = anchor.getAttribute('href') || '';
    if (href.startsWith('/api/documents/')) {
      event.preventDefault();
      const result = await window.fetch(href.split('#')[0]);
      const object = URL.createObjectURL(await result.blob());
      const download = document.createElement('a'); download.href = object; download.download = 'TEST-preview-source.txt'; download.click();
      setTimeout(() => URL.revokeObjectURL(object), 10000);
    } else if (href === '/service' || href === '/support') {
      event.preventDefault(); window.alert('TEST-просмотр показывает интерфейс. Полные информационные страницы доступны при запуске Python-сервера.');
    }
  }, true);
}
