from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import io
import logging
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlparse
import uuid
from xml.sax.saxutils import escape as xml_escape
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from .config import ROOT, settings, effective_mode
from .db import SessionLocal, get_session, init_db, utcnow
from .models import AppSetting, AuthSession, ChatMessage, Conversation, Document, DocumentPage, KnowledgeChunk, User
from .auth import AUTH_COOKIE, create_session, get_actor_scope, hash_password, optional_user, require_admin, require_staff, token_hash, user_dict, verify_password
from .knowledge import document_dict, ingest_document, search_knowledge
from . import ai, workflows

_indexer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='document-index')
_rate_lock = threading.Lock()
_rate_buckets: dict = defaultdict(deque)
_dummy_hash = hash_password(secrets.token_urlsafe(20))
# Inherit Uvicorn's configured console handler and INFO level on Windows too.
logger = logging.getLogger('uvicorn.error.posteks')


def rate_limit(request: Request, bucket: str, limit: int = 20):
    key = (bucket, request.client.host if request.client else 'unknown')
    now = time.monotonic()
    with _rate_lock:
        if len(_rate_buckets) > 2000:
            for item in list(_rate_buckets):
                if not _rate_buckets[item] or _rate_buckets[item][-1] < now - 120:
                    del _rate_buckets[item]
        values = _rate_buckets[key]
        while values and values[0] <= now - 60:
            values.popleft()
        if len(values) >= limit:
            raise HTTPException(429, 'Слишком много запросов. Повторите через минуту.', headers={'Retry-After': '60'})
        values.append(now)


@asynccontextmanager
async def lifespan(app):
    init_db()
    from .mail_sync import start_mail_worker, stop_mail_worker
    with SessionLocal() as session:
        mail_enabled = effective_mode(session) != 'TEST'
        pending = session.scalars(select(Document.id).where(Document.status.in_(['queued', 'processing']), Document.deleted == False)).all()
    if mail_enabled:
        start_mail_worker()
    for document_id in pending:
        _indexer.submit(ingest_document, document_id)
    try:
        yield
    finally:
        if mail_enabled:
            stop_mail_worker()


app = FastAPI(title='POSTEKS AI', version='0.2.0', lifespan=lifespan)


@app.middleware('http')
async def browser_security(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.monotonic()
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        origin = request.headers.get('origin')
        fetch_site = request.headers.get('sec-fetch-site', '')
        expected = str(request.base_url).rstrip('/')
        allowed = {v.strip().rstrip('/') for v in settings.allowed_origins.split(',') if v.strip()}
        if settings.public_base_url:
            allowed.add(settings.public_base_url)
        allowed.add(expected)
        if (origin and origin.rstrip('/') not in allowed) or fetch_site == 'cross-site':
            return JSONResponse({'detail': 'Запрос с другого сайта отклонён (CSRF)'}, status_code=403)
    response = await call_next(request)
    elapsed = round((time.monotonic() - started) * 1000)
    response.headers['X-Request-ID'] = request_id
    response.headers['X-Response-Time-Ms'] = str(elapsed)
    logger.info('request_id=%s method=%s path=%s status=%s latency_ms=%s', request_id,
                request.method, request.url.path, response.status_code, elapsed)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(self), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; script-src 'self'; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if any(request.url.path == prefix or request.url.path.startswith(prefix + '/') for prefix in ('/api', '/admin', '/chat', '/docs', '/redoc')):
        response.headers['X-Robots-Tag'] = 'noindex, noarchive'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    request_id = getattr(request.state, 'request_id', uuid.uuid4().hex)
    logger.error('request_id=%s error_type=%s', request_id, type(exc).__name__)
    return JSONResponse({'detail': 'Ошибка сервера. Сохраните код запроса и проверьте диагностику.',
                         'request_id': request_id}, status_code=500, headers={'X-Request-ID': request_id})


@app.exception_handler(StaleDataError)
async def changed_conversation(request: Request, exc: StaleDataError):
    request_id = getattr(request.state, 'request_id', uuid.uuid4().hex)
    return JSONResponse({'detail': 'Диалог изменён другим запросом. Обновите историю и повторите сообщение.',
                         'request_id': request_id}, status_code=409, headers={'X-Request-ID': request_id})


@app.get('/api/health')
def health(session: Session = Depends(get_session)):
    count = session.scalar(select(func.count(Document.id)).where(Document.status == 'indexed', Document.deleted == False)) or 0
    return {'status': 'ok', 'mode': effective_mode(session),
            'safe_mode': True, 'mail_send_enabled': False, 'documents_indexed': count,
            'ai_configured': bool(settings.openai_api_key)}


@app.get('/api/diagnostics')
def diagnostics(user: User = Depends(require_staff), session: Session = Depends(get_session)):
    from sqlalchemy.engine import make_url
    from .source_access import can_use_for_answer
    from .knowledge import _ocr_languages, IndexingError
    from .legacy_office import office_binary
    url = make_url(settings.database_url)
    count = lambda model, *conditions: session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
    docs = session.scalars(select(Document).where(Document.deleted.is_(False))).all()
    warnings = list(settings.configuration_warnings)
    missing = sum(not (settings.documents_dir / doc.storage_name).is_file() for doc in docs)
    if missing:
        warnings.append(f'Исходных файлов не найдено: {missing}. Восстановите папку documents из резервной копии.')
    if not docs:
        warnings.append('Выбрана пустая база знаний. Загрузите документы или выполните безопасный импорт; архив программы не содержит частные материалы.')
    else:
        if not any(can_use_for_answer(doc) for doc in docs):
            warnings.append('Документы пока не разрешены для клиентских ответов. Они доступны в AI Playground; администратор может разрешить ответы по выбранному руководству, оставив оригинал закрытым.')
    legacy = ROOT / 'posteks.db'
    if legacy.is_file():
        warnings.append('Обнаружен прежний posteks.db. Он не подключён к веб-сервису; выполните read-only аудит перед импортом.')
    from .ocr import diagnostic as ocr_diagnostic
    ocr = ocr_diagnostic()
    revision = session.execute(text('SELECT version_num FROM alembic_version')).scalar() if 'alembic_version' in __import__('sqlalchemy').inspect(session.bind).get_table_names() else None
    integrity = session.execute(text('PRAGMA quick_check(1)')).scalar() if url.get_backend_name() == 'sqlite' else 'not_checked'
    return {'version': app.version, 'project_root': str(ROOT), 'storage_dir': str(settings.storage_dir),
        'documents_dir': str(settings.documents_dir),
        'database': {'backend': url.get_backend_name(), 'path': url.database if url.get_backend_name() == 'sqlite' else None,
                     'revision': revision, 'integrity': integrity},
        'counts': {'documents': len(docs), 'indexed': sum(doc.status == 'indexed' for doc in docs),
                   'client_answer_documents': sum(can_use_for_answer(doc) for doc in docs) if docs else 0,
                   'public': sum(doc.visibility == 'public' for doc in docs), 'internal': sum(doc.visibility != 'public' for doc in docs),
                   'pages': count(DocumentPage), 'chunks': count(KnowledgeChunk), 'missing_originals': missing,
                   'inbox': count(workflows.InboxMessage), 'conversations': count(Conversation), 'cases': count(workflows.ServiceCase)},
        'indexer': {'pending': sum(doc.status == 'queued' for doc in docs), 'processing': sum(doc.status == 'processing' for doc in docs),
                    'errors': sum(doc.status == 'error' for doc in docs)}, 'ocr': ocr,
        'legacy': {'detected': legacy.is_file(), 'path': str(legacy) if legacy.is_file() else None, 'imported': False},
        'warnings': warnings, 'capabilities': {'legacy_office': bool(office_binary()), 'voice': bool(settings.openai_api_key), 'vision': bool(settings.openai_api_key)}}


@app.get('/api/auth/me')
def me(request: Request, response: Response, session: Session = Depends(get_session)):
    user = optional_user(request, session)
    if not user:
        get_actor_scope(request, response, session)
    return {'authenticated': bool(user), 'user': user_dict(user) if user else None}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


@app.post('/api/auth/login')
def login(body: LoginBody, request: Request, response: Response, session: Session = Depends(get_session)):
    rate_limit(request, 'login', 8)
    user = session.scalar(select(User).where(User.username == body.username.strip()))
    valid = verify_password(body.password, user.password_hash if user else _dummy_hash)
    if not user or not user.active or not valid:
        raise HTTPException(401, 'Неверное имя пользователя или пароль')
    create_session(user, session, response)
    return {'authenticated': True, 'user': user_dict(user)}


@app.post('/api/auth/logout')
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    token = request.cookies.get(AUTH_COOKIE, '')
    if token:
        session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        session.commit()
    response.delete_cookie(AUTH_COOKIE, path='/', secure=settings.cookie_secure, httponly=True, samesite='lax')
    return {'ok': True}


def has_staff_access(request: Request, session: Session) -> bool:
    user = optional_user(request, session)
    return bool(user and user.role in {'admin', 'operator', 'engineer'})


def visible_document(document_id: int, request: Request, session: Session):
    doc = session.get(Document, document_id)
    if not doc or doc.deleted or (doc.visibility != 'public' and not has_staff_access(request, session)):
        raise HTTPException(404, 'Документ не найден')
    return doc


@app.get('/api/documents')
def documents(user: User = Depends(require_staff), session: Session = Depends(get_session)):
    return [document_dict(d) for d in session.scalars(select(Document).where(Document.deleted == False).order_by(Document.created_at.desc())).all()]


_ALLOWED = {'.pdf', '.doc', '.docx', '.txt', '.md', '.xls', '.xlsx', '.png', '.jpg', '.jpeg', '.webp'}


def validate_magic(path: Path, suffix: str):
    with path.open('rb') as source:
        head = source.read(16)
    tests = {'.pdf': head.startswith(b'%PDF-'), '.docx': head.startswith(b'PK'), '.xlsx': head.startswith(b'PK'),
             '.doc': head.startswith(bytes.fromhex('d0cf11e0a1b11ae1')), '.xls': head.startswith(bytes.fromhex('d0cf11e0a1b11ae1')),
             '.png': head.startswith(b'\x89PNG\r\n\x1a\n'), '.jpg': head.startswith(b'\xff\xd8\xff'),
             '.jpeg': head.startswith(b'\xff\xd8\xff'), '.webp': head.startswith(b'RIFF') and head[8:12] == b'WEBP'}
    if suffix in tests and not tests[suffix]:
        raise HTTPException(422, 'Содержимое файла не соответствует расширению')
    if suffix in {'.txt', '.md'} and b'\x00' in head:
        raise HTTPException(422, 'Ожидается текстовый файл UTF-8')


@app.post('/api/documents')
def upload_documents(request: Request, files: list[UploadFile] = File(...), visibility: str = Form('internal'),
                     user: User = Depends(require_staff), session: Session = Depends(get_session)):
    rate_limit(request, 'upload', 20)
    if visibility not in {'internal', 'public'}:
        raise HTTPException(422, 'visibility: internal или public')
    if visibility == 'public' and user.role != 'admin':
        raise HTTPException(403, 'Разрешить документ для клиентской поддержки может только администратор')
    if not 1 <= len(files) <= 8:
        raise HTTPException(422, 'Загрузите от 1 до 8 файлов за запрос')
    output = []
    for upload in files:
        name = (upload.filename or 'document').replace('\\', '/').rsplit('/', 1)[-1][:400]
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED:
            raise HTTPException(415, 'Поддерживаются PDF, DOCX, TXT, MD, XLSX, JPG, PNG, WEBP')
        storage_name = uuid.uuid4().hex + suffix
        destination = settings.documents_dir / storage_name
        size = 0
        digest = hashlib.sha256()
        try:
            with destination.open('xb') as target:
                while block := upload.file.read(1024 * 1024):
                    size += len(block)
                    if size > settings.max_file_bytes:
                        raise HTTPException(413, 'Максимальный размер файла — 25 МБ')
                    digest.update(block)
                    target.write(block)
            if not size:
                raise HTTPException(422, 'Файл пуст')
            validate_magic(destination, suffix)
            duplicate = session.scalar(select(Document).where(Document.sha256 == digest.hexdigest(), Document.deleted == False))
            if duplicate:
                destination.unlink(missing_ok=True)
                data = document_dict(duplicate)
                data['duplicate'] = True
                output.append(data)
                continue
            document = Document(title=Path(name).stem, filename=name, storage_name=storage_name,
                sha256=digest.hexdigest(), mime_type=upload.content_type or 'application/octet-stream',
                size_bytes=size, visibility=visibility, status='queued')
            session.add(document)
            session.commit()
            workflows.record_event(session, user.username, 'document.upload', 'document', str(document.id), {'filename': name, 'visibility': visibility})
            session.commit()
            output.append(document_dict(document))
            _indexer.submit(ingest_document, document.id)
        except Exception:
            session.rollback()
            destination.unlink(missing_ok=True)
            raise
        finally:
            upload.file.close()
    return output


class DocumentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    visibility: str | None = None
    answer_access: str | None = None
    category: str | None = Field(default=None, max_length=100)
    models: list[str] | None = Field(default=None, max_length=15)


@app.patch('/api/documents/{document_id}')
def update_document(document_id: int, body: DocumentPatch, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc or doc.deleted:
        raise HTTPException(404, 'Документ не найден')
    if body.visibility and body.visibility not in {'public', 'internal'}:
        raise HTTPException(422, 'visibility: internal или public')
    if body.visibility is not None and body.visibility != doc.visibility and user.role != 'admin':
        raise HTTPException(403, 'Изменять доступ к документам может только администратор')
    from .source_access import ANSWER_ACCESS, unreviewed_history
    if body.answer_access is not None:
        if body.answer_access not in ANSWER_ACCESS:
            raise HTTPException(422, 'answer_access: staff_only или client_answers')
        if body.answer_access != doc.answer_access and user.role != 'admin':
            raise HTTPException(403, 'Разрешать или запрещать использование документа в клиентских ответах может только администратор')
        if body.answer_access == 'client_answers' and unreviewed_history(doc, session):
            raise HTTPException(409, 'Непроверенную переписку нельзя разрешить для клиентских ответов. Подготовьте отдельное очищенное решение и передайте его на проверку.')
    imported_history = session.scalar(select(AppSetting.key).where(AppSetting.key.like('history_import:%'),
        AppSetting.value['document_id'].as_integer() == doc.id))
    if body.visibility == 'public' and (unreviewed_history(doc, session) or imported_history or
            (doc.indexing_details or {}).get('source_kind') == 'unreviewed_history'):
        raise HTTPException(409, 'История переписки остаётся внутренней. Подготовьте отдельное очищенное решение и передайте его на проверку.')
    for field, value in body.model_dump(exclude_none=True).items():
        if field == 'models':
            from .knowledge import normalize_model
            value = list(dict.fromkeys(normalize_model(m) for m in value if m.strip()))
        setattr(doc, field, value)
    workflows.record_event(session, user.username, 'document.update', 'document', str(doc.id), body.model_dump(exclude_none=True))
    session.commit()
    return document_dict(doc)


@app.post('/api/documents/{document_id}/reindex')
def reindex(document_id: int, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc or doc.deleted:
        raise HTTPException(404, 'Документ не найден')
    if doc.status in {'queued', 'processing'}:
        return document_dict(doc)
    doc.status, doc.error = 'queued', None
    doc.version += 1
    session.commit()
    _indexer.submit(ingest_document, doc.id)
    return document_dict(doc)


@app.delete('/api/documents/{document_id}')
def delete_document(document_id: int, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc or doc.deleted:
        raise HTTPException(404, 'Документ не найден')
    doc.deleted = True
    workflows.record_event(session, user.username, 'document.delete', 'document', str(doc.id), {})
    session.commit()
    return {'ok': True}


@app.get('/api/documents/{document_id}/file')
def document_file(document_id: int, request: Request, session: Session = Depends(get_session)):
    doc = visible_document(document_id, request, session)
    path = settings.documents_dir / doc.storage_name
    if not path.is_file():
        raise HTTPException(404, 'Исходный файл недоступен')
    media = 'application/pdf' if path.suffix.lower() == '.pdf' else None
    return FileResponse(path, filename=doc.filename, media_type=media, content_disposition_type='inline')


@app.get('/api/documents/{document_id}/pages/{page}')
def document_page(document_id: int, page: int, request: Request, session: Session = Depends(get_session)):
    doc = visible_document(document_id, request, session)
    value = session.scalar(select(DocumentPage).where(DocumentPage.document_id == doc.id, DocumentPage.page == page))
    if not value:
        raise HTTPException(404, 'Страница не найдена')
    return {'document_id': doc.id, 'title': doc.title, 'page': page, 'total_pages': doc.pages, 'text': value.text, 'layout': value.layout or {},
            'image_url': f'/api/documents/{doc.id}/pages/{page}/image' if Path(doc.storage_name).suffix.lower() == '.pdf' else None}


@app.get('/api/documents/{document_id}/pages/{page}/image')
def page_image(document_id: int, page: int, request: Request, session: Session = Depends(get_session)):
    doc = visible_document(document_id, request, session)
    path = settings.documents_dir / doc.storage_name
    if page < 1 or page > doc.pages:
        raise HTTPException(404, 'Страница не найдена')
    if path.suffix.lower() != '.pdf':
        raise HTTPException(415, 'Предпросмотр страницы поддерживается для PDF')
    try:
        import pymupdf as fitz
        with fitz.open(path) as pdf:
            item = pdf[page - 1]
            rect = item.rect
            scale = min(1.7, math.sqrt(4_000_000 / max(rect.width * rect.height, 1)))
            image = item.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Response(image.tobytes('png'), media_type='image/png')
    except Exception:
        raise HTTPException(422, 'Не удалось отрисовать страницу PDF')


@app.get('/api/knowledge/search')
def knowledge_search(request: Request, q: str = '', model: str | None = None, document_id: int | None = None,
                     session: Session = Depends(get_session)):
    if not q.strip() or len(q) > 4000:
        raise HTTPException(422, 'Введите запрос длиной до 4000 символов')
    rate_limit(request, 'search', 60)
    staff = has_staff_access(request, session)
    rows = search_knowledge(session, q, model=model, document_id=document_id, include_internal=staff)
    return {'results': rows, 'mode': 'local'}


@app.get('/api/knowledge/solutions/{solution_id}')
def public_solution(solution_id: int, request: Request, session: Session = Depends(get_session)):
    row = session.get(workflows.VerifiedSolution, solution_id)
    staff = has_staff_access(request, session)
    if not row or (not staff and (row.status != 'VERIFIED' or row.visibility != 'public')):
        raise HTTPException(404, 'Решение не найдено')
    return {'id': row.id, 'title': row.title, 'equipment_model': row.equipment_model,
            'symptoms': row.symptoms, 'actions': row.actions, 'limitations': row.limitations,
            'source_type': 'verified_solution'}


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    conversation_id: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=100)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)


def message_dict(message: ChatMessage, session: Session | None = None, staff: bool = True):
    content, sources = message.content, list(message.sources or [])
    if not staff and session is not None and message.role == 'assistant':
        from .source_access import can_use_for_answer, client_source_view
        visible_sources = []
        for source in sources:
            if source.get('document_id'):
                doc = session.get(Document, source['document_id'], populate_existing=True)
                allowed = can_use_for_answer(doc)
                if allowed:
                    source = client_source_view(source, doc)
            elif source.get('solution_id'):
                solution = session.get(workflows.VerifiedSolution, source['solution_id'], populate_existing=True)
                allowed = bool(solution and solution.status == 'VERIFIED' and solution.visibility == 'public')
            else:
                allowed = False
            if not allowed:
                content, sources = 'Источники этого ответа больше не доступны для публичного просмотра. Задайте вопрос снова.', []
                break
            visible_sources.append(source)
        else:
            sources = visible_sources
    attachment_record = session.get(AppSetting, 'chat_message_attachments:' + str(message.id)) if session is not None else None
    metadata = session.get(AppSetting, 'chat_message_meta:' + str(message.id)) if session is not None else None
    return {'id': message.id, 'role': message.role, 'content': content,
            'sources': sources, 'attachments': attachment_record.value.get('attachments', []) if attachment_record else [],
            **(metadata.value if metadata else {}),
            'created_at': message.created_at.isoformat() + 'Z'}


@app.post('/api/chat')
def chat(body: ChatBody, request: Request, response: Response, session: Session = Depends(get_session)):
    from .orchestrator import conversation_lock
    from sqlalchemy.orm.exc import StaleDataError
    lock_key = body.conversation_id or uuid.uuid4().hex
    try:
        with conversation_lock(lock_key):
            return _chat_locked(body, request, response, session)
    except StaleDataError:
        session.rollback()
        raise HTTPException(409, 'Диалог изменён другим запросом. Обновите историю и повторите сообщение.')


def _chat_locked(body: ChatBody, request: Request, response: Response, session: Session):
    rate_limit(request, 'chat', 20)
    if not body.message.strip():
        raise HTTPException(422, 'Сообщение пустое')
    scope = get_actor_scope(request, response, session)
    conversation = session.get(Conversation, body.conversation_id) if body.conversation_id else None
    if body.conversation_id and (not conversation or conversation.owner_scope != scope):
        raise HTTPException(404, 'Диалог не найден')
    if not conversation:
        conversation = Conversation(id=str(uuid.uuid4()), owner_scope=scope, title=body.message.strip()[:100])
        session.add(conversation)
        session.commit()
    from .media import owned_attachments
    attachments = owned_attachments(session, body.attachment_ids, scope, conversation.id)
    history = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.id.desc()).limit(20)).all()
    user_message = ChatMessage(conversation_id=conversation.id, role='user', content=body.message, sources=[])
    session.add(user_message)
    session.flush()
    if attachments:
        session.add(AppSetting(key='chat_message_attachments:' + str(user_message.id), value={'attachments': attachments}))
    session.commit()
    result = ai.answer(session, body.message, equipment_model=body.model,
        include_internal=False, conversation=conversation, attachments=attachments,
        require_model=not (settings.reference_test_mode and effective_mode(session) == 'TEST'),
        history=[{'role': m.role, 'content': message_dict(m, session, False)['content'][:3000]} for m in reversed(history)])
    assistant = ChatMessage(conversation_id=conversation.id, role='assistant', content=result.pop('content'), sources=result.pop('sources'))
    session.add(assistant)
    session.flush()
    session.add(AppSetting(key='chat_message_meta:' + str(assistant.id), value={
        'engine': result.get('engine'), 'error': result.get('error'),
        'model': result.get('model'), 'provider_response_id': result.get('provider_response_id')}))
    conversation.updated_at = utcnow()
    session.commit()
    return {'conversation_id': conversation.id, 'message': message_dict(assistant, session, False),
            **result, 'request_id': getattr(request.state, 'request_id', None)}


@app.get('/api/conversations')
def conversations(request: Request, response: Response, session: Session = Depends(get_session)):
    scope = get_actor_scope(request, response, session)
    return [{'id': c.id, 'title': c.title, 'updated_at': c.updated_at.isoformat() + 'Z'} for c in
            session.scalars(select(Conversation).where(Conversation.owner_scope == scope).order_by(Conversation.updated_at.desc()).limit(100)).all()]


@app.get('/api/conversations/{conversation_id}')
def conversation_detail(conversation_id: str, request: Request, response: Response, session: Session = Depends(get_session)):
    scope = get_actor_scope(request, response, session)
    conversation = session.get(Conversation, conversation_id)
    if not conversation or conversation.owner_scope != scope:
        raise HTTPException(404, 'Диалог не найден')
    messages = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.id)).all()
    from .orchestrator import get_state, public_state
    return {'id': conversation.id, 'title': conversation.title, 'messages': [message_dict(m, session, False) for m in messages],
            'state': public_state(get_state(session, conversation))}


@app.delete('/api/conversations/{conversation_id}')
def conversation_delete(conversation_id: str, request: Request, response: Response, session: Session = Depends(get_session)):
    scope = get_actor_scope(request, response, session)
    conversation = session.get(Conversation, conversation_id)
    if not conversation or conversation.owner_scope != scope:
        raise HTTPException(404, 'Диалог не найден')
    session.delete(conversation)
    session.commit()
    return {'ok': True}


from .playground import router as playground_router
app.include_router(playground_router)

@app.get('/api/settings')
def get_settings(user: User = Depends(require_staff), session: Session = Depends(get_session)):
    return {'mode': effective_mode(session), 'safe_mode': True,
            'mail_send_enabled': False, 'ai_configured': bool(settings.openai_api_key),
            'models': ai.model_config(session), 'max_file_mb': 25,
            'max_ai_daily_tokens': settings.max_ai_daily_tokens,
            'credentials': {'openai': bool(settings.openai_api_key)},
            'capabilities': {'local_search': True, 'ai_answers': bool(settings.openai_api_key),
                             'outgoing_mail': False, 'native_mobile': False}}


class SettingsPatch(BaseModel):
    mode: str | None = None
    models: dict[str, str] | None = None


@app.patch('/api/settings')
def patch_settings(body: SettingsPatch, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    if body.mode:
        mode = body.mode.upper()
        if mode not in {'TEST', 'SAFE', 'PRODUCTION'}:
            raise HTTPException(422, 'Режим TEST, SAFE или PRODUCTION')
        if mode == 'PRODUCTION' and not settings.cookie_secure:
            raise HTTPException(422, 'Для PRODUCTION необходим COOKIE_SECURE=true и HTTPS')
        row = session.get(AppSetting, 'mode')
        if row:
            row.value = {'mode': mode}
        else:
            session.add(AppSetting(key='mode', value={'mode': mode}))
    if body.models is not None:
        if any(k not in ai.MODEL_KEYS or not re.fullmatch(r'[a-zA-Z0-9._:-]{1,100}', v) for k, v in body.models.items()):
            raise HTTPException(422, 'Неизвестное назначение или некорректное имя модели')
        values = ai.model_config(session) | body.models
        row = session.get(AppSetting, 'models')
        if row:
            row.value = values
        else:
            session.add(AppSetting(key='models', value=values))
    workflows.record_event(session, user.username, 'settings.update', 'settings', 'app', body.model_dump(exclude_none=True))
    session.commit()
    return get_settings(user, session)


@app.post('/api/settings/check-openai')
def check_openai(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    rate_limit(request, 'openai-check', 5)
    return ai.check_openai(session)


app.include_router(workflows.router)
from .mail_organization import router as mail_views_router
from .mail_ai import router as mail_ai_router
from .semantic_search import router as semantic_router
app.include_router(mail_views_router)
app.include_router(mail_ai_router)
app.include_router(semantic_router)
from .equipment_context import router as equipment_context_router
app.include_router(equipment_context_router)
from .escalations import router as escalation_router
from .history_import import router as history_router
from .media import router as media_router
app.include_router(escalation_router)
app.include_router(history_router)
app.include_router(media_router)
from .public_pages import router as public_router
app.include_router(public_router)


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    value = 'User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\n'
    if settings.public_base_url:
        value += f'Sitemap: {settings.public_base_url}/sitemap.xml\n'
    return value


@app.get('/sitemap.xml')
def sitemap():
    urls = ''.join(f'<url><loc>{xml_escape(settings.public_base_url + path)}</loc></url>' for path in ['/', '/service', '/support']) if settings.public_base_url.startswith('https://') else ''
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', media_type='application/xml')


_dist = ROOT / 'frontend' / 'dist'
if (_dist / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=_dist / 'assets'), name='assets')


@app.get('/{full_path:path}', include_in_schema=False)
def frontend(full_path: str):
    if full_path == 'api' or full_path.startswith('api/'):
        raise HTTPException(404, 'API endpoint not found')
    path = (_dist / full_path).resolve()
    if path.is_relative_to(_dist.resolve()) and path.is_file():
        return FileResponse(path)
    if (_dist / 'index.html').is_file():
        return FileResponse(_dist / 'index.html')
    return JSONResponse({'detail': 'Frontend ещё не собран. Выполните npm install и npm run build в frontend.'}, status_code=503)
