"""Private service workflows. Mail sends deliberately remain disabled.

Imported MIME is untrusted data. Only decoded text and attachment metadata are
exposed; no attachment is executed and no mail HTML is rendered by the server.
"""
from __future__ import annotations

import hashlib
import imaplib
import io
import os
import re
import ssl
import unicodedata
from datetime import date, datetime
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Literal
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, func, or_, select
from sqlalchemy.orm import Session, relationship, load_only, defer
from sqlalchemy.exc import IntegrityError

from server.auth import require_admin, require_staff
from server.config import effective_mode, settings
from server.db import Base, get_session, utcnow
from server import mail_models  # register private mail tables and relationships

router = APIRouter(prefix="/api", dependencies=[Depends(require_staff)])
MAX_MAIL_BYTES = 5 * 1024 * 1024
MAX_WORKBOOK_BYTES = 15 * 1024 * 1024
MAX_TEXT = 100_000


class ServiceCase(Base):
    __tablename__ = "service_cases"
    id = Column(Integer, primary_key=True)
    service_number = Column(String(80), unique=True, nullable=True)
    title = Column(String(240), nullable=False)
    company = Column(String(240), default="", nullable=False)
    contact = Column(String(240), default="", nullable=False)
    email = Column(String(320), default="", nullable=False)
    equipment_model = Column(String(120), default="", nullable=False)
    serial_number = Column(String(160), default="", nullable=False)
    status = Column(String(24), default="new", nullable=False, index=True)
    priority = Column(String(16), default="normal", nullable=False)
    source = Column(String(30), default="OPERATOR", nullable=False)
    kind = Column(String(24), default="service", nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    messages = relationship("CaseMessage", cascade="all, delete-orphan", order_by="CaseMessage.id")
    tasks = relationship("InternalTask", cascade="all, delete-orphan", order_by="InternalTask.id")


class CaseMessage(Base):
    __tablename__ = "case_messages"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("service_cases.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    kind = Column(String(20), default="internal", nullable=False)
    author = Column(String(160), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class InternalTask(Base):
    __tablename__ = "internal_tasks"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("service_cases.id"), nullable=False, index=True)
    title = Column(String(400), nullable=False)
    department = Column(String(160), nullable=False)
    assignee = Column(String(160), default="", nullable=False)
    status = Column(String(24), default="open", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    id = Column(Integer, primary_key=True)
    message_id = Column(String(998), unique=True, nullable=False)
    content_hash = Column(String(64), unique=True, nullable=False)
    case_id = Column(Integer, ForeignKey("service_cases.id"), nullable=True, index=True)
    subject = Column(String(998), nullable=False)
    sender = Column(String(320), nullable=False)
    sender_name = Column(String(240), default="", nullable=False)
    recipients = Column(JSON, default=list, nullable=False)
    body = Column(Text, default="", nullable=False)
    references = Column(JSON, default=list, nullable=False)
    attachments = Column(JSON, default=list, nullable=False)
    raw_mime = Column(LargeBinary, nullable=False)
    source = Column(String(30), default="TEST_IMPORT", nullable=False)
    sent_at = Column(DateTime, nullable=True)
    local_read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Approval(Base):
    __tablename__ = "mail_approvals"
    id = Column(Integer, primary_key=True)
    inbox_id = Column(Integer, ForeignKey("inbox_messages.id"), nullable=False)
    case_id = Column(Integer, ForeignKey("service_cases.id"), nullable=True)
    subject = Column(String(998), nullable=False)
    recipient = Column(String(320), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(24), default="PENDING", nullable=False, index=True)
    created_by = Column(String(160), nullable=False)
    approved_by = Column(String(160), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Equipment(Base):
    __tablename__ = "equipment"
    id = Column(Integer, primary_key=True)
    serial_number = Column(String(240), nullable=False)
    normalized_serial = Column(String(240), unique=True, nullable=False, index=True)
    model = Column(String(240), default="", nullable=False)
    project = Column(String(240), default="", nullable=False)
    name = Column(String(500), default="", nullable=False)
    shipped_at = Column(String(80), nullable=True)
    customer = Column(String(240), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class VerifiedSolution(Base):
    __tablename__ = "verified_solutions"
    id = Column(Integer, primary_key=True)
    title = Column(String(240), nullable=False)
    equipment_model = Column(String(120), default="", nullable=False, index=True)
    symptoms = Column(Text, default="", nullable=False)
    actions = Column(Text, default="", nullable=False)
    limitations = Column(Text, default="", nullable=False)
    source = Column(Text, default="", nullable=False)
    customer_confirmed = Column(Boolean, default=False, nullable=False)
    status = Column(String(24), default="CANDIDATE", nullable=False, index=True)
    visibility = Column(String(20), default="internal", nullable=False)
    verified_by = Column(String(160), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    actor = Column(String(160), nullable=False)
    action = Column(String(80), nullable=False)
    entity_type = Column(String(80), nullable=False)
    entity_id = Column(String(80), nullable=False)
    detail = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False, unique=True)
    responsibility = Column(String(1000), default="", nullable=False)
    email = Column(String(320), default="", nullable=False)
    backup_assignee = Column(String(160), default="", nullable=False)
    active = Column(Boolean, default=True, nullable=False)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CaseCreate(InputModel):
    title: str = Field(min_length=1, max_length=240)
    company: str = Field(default="", max_length=240)
    contact: str = Field(default="", max_length=240)
    email: str = Field(default="", max_length=320)
    equipment_model: str = Field(default="", max_length=120)
    serial_number: str = Field(default="", max_length=160)
    priority: Literal["normal", "high"] = "normal"


class CaseUpdate(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    service_number: str | None = Field(default=None, max_length=80)
    company: str | None = Field(default=None, max_length=240)
    contact: str | None = Field(default=None, max_length=240)
    email: str | None = Field(default=None, max_length=320)
    equipment_model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=160)
    status: Literal["new", "in_progress", "waiting", "resolved"] | None = None
    priority: Literal["normal", "high"] | None = None


class MessageCreate(InputModel):
    body: str = Field(min_length=1, max_length=MAX_TEXT)
    kind: Literal["internal", "client"] = "internal"


class TaskCreate(InputModel):
    title: str = Field(min_length=1, max_length=400)
    department: str = Field(min_length=1, max_length=160)
    assignee: str = Field(default="", max_length=160)


class TaskUpdate(InputModel):
    status: Literal["open", "in_progress", "waiting", "done", "cancelled"]


class DraftCreate(InputModel):
    body: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    recipient: str | None = Field(default=None, max_length=320)
    reply_goal: str = Field(default='', max_length=2000)


class ApprovalUpdate(InputModel):
    body: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    recipient: str | None = Field(default=None, min_length=3, max_length=320)
    status: Literal["PENDING", "APPROVED", "REJECTED"] | None = None


class SolutionCreate(InputModel):
    title: str = Field(min_length=1, max_length=240)
    equipment_model: str = Field(default="", max_length=120)
    symptoms: str = Field(default="", max_length=20_000)
    actions: str = Field(default="", max_length=20_000)
    limitations: str = Field(default="", max_length=20_000)
    source: str = Field(default="", max_length=4000)
    customer_confirmed: bool = False
    visibility: Literal["internal", "public"] = "internal"
    status: Literal["CANDIDATE"] = "CANDIDATE"


class SolutionUpdate(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    equipment_model: str | None = Field(default=None, max_length=120)
    symptoms: str | None = Field(default=None, max_length=20_000)
    actions: str | None = Field(default=None, max_length=20_000)
    limitations: str | None = Field(default=None, max_length=20_000)
    source: str | None = Field(default=None, max_length=4000)
    customer_confirmed: bool | None = None
    visibility: Literal["internal", "public"] | None = None
    status: Literal["CANDIDATE", "NEEDS_REVIEW", "VERIFIED", "REJECTED", "ARCHIVED"] | None = None


class DepartmentCreate(InputModel):
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(default="", max_length=1000)
    email: str = Field(default="", max_length=320)
    backup_assignee: str = Field(default="", max_length=160)
    active: bool = True


def _actor(actor) -> str:
    return str(getattr(actor, "username", actor))[:160]


def record_event(session: Session, actor, action: str, entity_type: str, entity_id, detail=None):
    """Record metadata only. Never log message contents, credentials, or tokens."""
    safe_detail = {}
    for key, value in (detail or {}).items():
        if any(token in key.lower() for token in ("password", "token", "secret", "credential", "body", "content")):
            continue
        if isinstance(value, (bool, int, float, type(None))):
            safe_detail[str(key)[:80]] = value
        elif isinstance(value, str):
            safe_detail[str(key)[:80]] = value[:500]
        elif isinstance(value, (list, tuple)):
            safe_items = []
            for item in value[:100]:
                if isinstance(item, dict):
                    safe_items.append({str(k)[:80]: v if isinstance(v, (int, bool, float, type(None))) else str(v)[:500] for k, v in item.items() if not any(token in str(k).lower() for token in ("password", "token", "secret", "credential", "body", "content"))})
                else:
                    safe_items.append(str(item)[:80])
            safe_detail[str(key)[:80]] = safe_items
    event = AuditEvent(actor=_actor(actor), action=action[:80], entity_type=entity_type[:80], entity_id=str(entity_id)[:80], detail=safe_detail)
    session.add(event)
    return event


def _get(session, model, object_id):
    item = session.get(model, object_id)
    if item is None:
        raise HTTPException(404, "Объект не найден")
    return item


def _iso(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) and value.tzinfo is None else value.isoformat() if isinstance(value, (date, datetime)) else value


def _dict(item, fields):
    return {name: _iso(getattr(item, name)) for name in fields.split()}


def serialize_case(item):
    result = _dict(item, "id service_number title company contact email equipment_model serial_number status priority source kind created_at updated_at")
    result["messages"] = [_dict(m, "id body kind author created_at") for m in item.messages]
    result["tasks"] = [_dict(t, "id case_id title department assignee status created_at updated_at") for t in item.tasks]
    return result


def serialize_mail(item):
    result = _dict(item, "id message_id case_id subject sender sender_name recipients body references attachments source sent_at created_at")
    locations = getattr(item, 'mail_locations', [])
    result['folders'] = list(dict.fromkeys(location.folder for location in locations))
    from .mail_sync import mail_config
    username = mail_config()['username'].casefold()
    result['direction'] = 'outgoing' if username and item.sender.casefold() == username or (locations and all(location.direction == 'outgoing' for location in locations)) else 'incoming'
    from .mail_organization import mail_metadata
    result.update(mail_metadata(item))
    from .history_import import _labels
    result['content_categories'] = _labels(item.subject + '\n' + item.body)
    result['categories_verified'] = False
    result['archival'] = item.source == 'IMAP_HISTORY'
    return result


def serialize_approval(item):
    result = _dict(item, "id inbox_id case_id subject recipient body status created_by approved_by created_at updated_at")
    result["sending_enabled"] = False
    if item.body.strip().startswith(('Сведения сохранены в этом диалоге.', 'Я сохранил сведения.')):
        result['needs_review'] = True
        result['warning'] = 'Черновик прежней версии содержит ответ чата. Пересоздайте ответ по контексту письма; автоматическое исправление не выполнялось.'
    return result


def serialize_equipment(item):
    result = _dict(item, "id serial_number model project name shipped_at customer")
    result["warranty_status"] = "unknown"
    return result


def serialize_solution(item):
    return _dict(item, "id title equipment_model symptoms actions limitations source customer_confirmed status visibility verified_by created_at updated_at")


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session)):
    count = lambda model, *where: session.scalar(select(func.count()).select_from(model).where(*where)) or 0
    document_table = Base.metadata.tables.get("documents")
    document_count = session.scalar(select(func.count()).select_from(document_table)) if document_table is not None else 0
    return {"counts": {"cases": count(ServiceCase, ServiceCase.kind == "service"), "open_cases": count(ServiceCase, ServiceCase.kind == "service", ServiceCase.status != "resolved"), "pending_approvals": count(Approval, Approval.status == "PENDING"), "documents": document_count, "equipment": count(Equipment), "messages": count(InboxMessage), "verified_solutions": count(VerifiedSolution, VerifiedSolution.status == "VERIFIED")}, "recent_events": [_dict(e, "id actor action entity_type entity_id detail created_at") for e in session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(12))]}


@router.get("/cases")
def list_cases(kind: Literal['service', 'correspondence', 'all'] = 'service', session: Session = Depends(get_session)):
    query = select(ServiceCase)
    if kind != 'all':
        query = query.where(ServiceCase.kind == kind)
    return [serialize_case(item) for item in session.scalars(query.order_by(ServiceCase.updated_at.desc()).limit(500))]


@router.post("/cases", status_code=201)
def create_case(data: CaseCreate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = ServiceCase(**data.model_dump())
    session.add(item)
    session.flush()
    record_event(session, actor, "case.created", "case", item.id)
    session.commit()
    return serialize_case(item)


@router.get("/cases/{case_id}")
def get_case(case_id: int, session: Session = Depends(get_session)):
    return serialize_case(_get(session, ServiceCase, case_id))


@router.patch("/cases/{case_id}")
def update_case(case_id: int, data: CaseUpdate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = _get(session, ServiceCase, case_id)
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for key, value in changes.items() if key != "service_number"):
        raise HTTPException(422, "Поле не может быть пустым")
    if "service_number" in changes:
        changes["service_number"] = changes["service_number"] or None
        if changes["service_number"] and session.scalar(select(ServiceCase.id).where(ServiceCase.service_number == changes["service_number"], ServiceCase.id != item.id)):
            raise HTTPException(409, "Сервисный номер уже назначен другому обращению")
    for key, value in changes.items():
        setattr(item, key, value)
    item.updated_at = utcnow()
    record_event(session, actor, "case.updated", "case", item.id, {"fields": list(changes)})
    session.commit()
    return serialize_case(item)


@router.post("/cases/{case_id}/messages", status_code=201)
def add_case_message(case_id: int, data: MessageCreate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    case = _get(session, ServiceCase, case_id)
    # Even a client-labelled entry is a local operator record, never an outbound action.
    item = CaseMessage(case_id=case.id, body=data.body, kind=data.kind, author=_actor(actor))
    session.add(item)
    case.updated_at = utcnow()
    session.flush()
    record_event(session, actor, "case.message_recorded", "case", case.id, {"kind": data.kind, "sent": False})
    session.commit()
    return {**_dict(item, "id case_id body kind author created_at"), "sent": False}


@router.post("/cases/{case_id}/tasks", status_code=201)
def add_task(case_id: int, data: TaskCreate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    case = _get(session, ServiceCase, case_id)
    item = InternalTask(case_id=case.id, **data.model_dump())
    session.add(item)
    case.updated_at = utcnow()
    session.flush()
    record_event(session, actor, "task.created", "task", item.id, {"case_id": case.id, "department": item.department})
    session.commit()
    return _dict(item, "id case_id title department assignee status created_at updated_at")


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, data: TaskUpdate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = _get(session, InternalTask, task_id)
    item.status = data.status
    record_event(session, actor, "task.updated", "task", item.id, {"status": data.status})
    session.commit()
    return _dict(item, "id case_id title department assignee status created_at updated_at")


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object"):
            self.skip += 1
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _message_ids(value: str):
    return [part.strip() for part in re.findall(r"<[^<>\r\n]{1,994}>", value)][:100]


def parse_eml(raw: bytes, max_bytes=MAX_MAIL_BYTES):
    if len(raw) > max_bytes:
        raise ValueError("Письмо превышает допустимый размер")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects and not message.keys():
        raise ValueError("Некорректный формат EML")
    if not message.get("From"):
        raise ValueError("EML должен содержать From")
    digest = hashlib.sha256(raw).hexdigest()
    ids = _message_ids(str(message.get("Message-ID", "")))
    message_id = ids[0] if ids else f"<sha256-{digest}@local-import>"
    senders = getaddresses([str(message.get("From", ""))])
    sender_name, sender = senders[0] if senders else ("", "")
    plain, html, attachments = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            filename = str(part.get_filename() or "attachment").replace("\\", "/")
            payload = part.get_payload(decode=True) or b""
            attachments.append({"name": PurePosixPath(filename).name[:240], "content_type": part.get_content_type(), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "available": False})
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html).append(text[:MAX_TEXT])
    body = "\n".join(plain)
    if not body and html:
        parser = _PlainHTML()
        parser.feed("\n".join(html))
        body = "".join(parser.parts)
    refs = _message_ids(str(message.get("References", "")) + " " + str(message.get("In-Reply-To", "")))
    sent_at = None
    try:
        if message.get("Date"):
            parsed = parsedate_to_datetime(str(message["Date"]))
            from datetime import timezone
            sent_at = parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError, OverflowError):
        pass
    return {"message_id": message_id, "content_hash": digest, "subject": str(message.get("Subject") or "(без темы)")[:998], "sender": sender[:320], "sender_name": sender_name[:240], "recipients": [address[:320] for _, address in getaddresses([str(message.get("To", ""))])][:100], "body": body[:MAX_TEXT], "references": list(dict.fromkeys(refs)), "attachments": attachments[:100], "raw_mime": raw, "sent_at": sent_at, "source": "TEST_IMPORT"}


def import_eml_batch(session: Session, files: list[tuple[str, bytes]], actor, *, source='TEST_IMPORT', commit=True, max_bytes=MAX_MAIL_BYTES, direction='incoming'):
    imported, duplicates, errors, warnings = [], 0, [], []
    for filename, raw in files:
        try:
            if not filename.lower().endswith(".eml"):
                raise ValueError("Поддерживается только .eml")
            fields = parse_eml(raw, max_bytes=max_bytes)
            fields['source'] = source
            if session.scalar(select(InboxMessage.id).where(or_(InboxMessage.message_id == fields["message_id"], InboxMessage.content_hash == fields["content_hash"]))):
                duplicates += 1
                continue
            item = InboxMessage(**fields)
            session.add(item)
            session.flush()
            imported.append(item)
        except (ValueError, MessageError, RecursionError) as error:
            errors.append({"file": PurePosixPath(filename.replace("\\", "/")).name[:240], "error": str(error) if isinstance(error, ValueError) else 'Некорректное MIME-письмо'})
    # Resolve after the batch is parsed: a reply may precede its root in the upload.
    known = {item.message_id: item for item in imported}
    def reverse_links(ref, excluding):
        candidates = session.scalars(select(InboxMessage).options(load_only(InboxMessage.id, InboxMessage.case_id, InboxMessage.references)).where(InboxMessage.case_id.is_not(None), InboxMessage.id != excluding, InboxMessage.references.cast(Text).contains(ref, autoescape=True)))
        return {candidate.case_id for candidate in candidates if ref in (candidate.references or [])}
    def assign_case(item, visited):
        if item.case_id:
            return item.case_id
        if item.message_id in visited:
            return None
        linked = set()
        for ref in reversed(item.references):
            parent = known.get(ref) or session.scalar(select(InboxMessage).options(load_only(InboxMessage.id, InboxMessage.message_id, InboxMessage.case_id, InboxMessage.references, InboxMessage.subject, InboxMessage.sender_name, InboxMessage.sender)).where(InboxMessage.message_id == ref))
            if parent is not None and parent is not item:
                case_id = assign_case(parent, visited | {item.message_id})
                if case_id:
                    linked.add(case_id)
            elif parent is None:
                linked.update(reverse_links(ref, item.id))
        linked.update(reverse_links(item.message_id, item.id))
        if len(linked) == 1:
            item.case_id = next(iter(linked))
        else:
            case = ServiceCase(title=item.subject[:240] or "Импорт EML", contact=item.sender_name, email=item.sender, source=source, kind='correspondence')
            session.add(case)
            session.flush()
            item.case_id = case.id
            if len(linked) > 1:
                warnings.append({"file": item.message_id, "error": "Ссылки указывают на разные обращения; цепочки не объединены"})
        return item.case_id
    for item in imported:
        assign_case(item, set())
        session.add(CaseMessage(case_id=item.case_id, body=item.body, kind='internal' if direction == 'outgoing' else 'client', author=item.sender or 'IMAP'))
        from .mail_sync import store_mail_attachments
        store_mail_attachments(session, item)
        record_event(session, actor, "mail.test_imported" if source == 'TEST_IMPORT' else 'mail.imported', "inbox", item.id, {"case_id": item.case_id, "attachment_count": len(item.attachments), 'source': source})
    if commit:
        session.commit()
    else:
        session.flush()
    return {"imported": len(imported), "duplicates": duplicates, "errors": errors, 'warnings': warnings, "source": source, "sending_enabled": False}


@router.get("/inbox")
def inbox(q: str = '', folder: str = '', direction: Literal['incoming', 'outgoing', 'all'] = 'all', session: Session = Depends(get_session)):
    query = select(InboxMessage).options(defer(InboxMessage.raw_mime))
    if q.strip():
        text = q.strip()[:300]
        query = query.where(or_(InboxMessage.subject.contains(text, autoescape=True), InboxMessage.body.contains(text, autoescape=True), InboxMessage.sender.contains(text, autoescape=True)))
    if folder:
        query = query.where(InboxMessage.id.in_(select(mail_models.MailRemoteMessage.inbox_id).where(mail_models.MailRemoteMessage.folder == folder[:512])))
    if direction != 'all':
        remote = select(mail_models.MailRemoteMessage.inbox_id).where(mail_models.MailRemoteMessage.inbox_id.is_not(None))
        matching = remote.where(mail_models.MailRemoteMessage.direction == direction)
        clause = InboxMessage.id.in_(matching)
        query = query.where(or_(clause, InboxMessage.id.not_in(remote)) if direction == 'incoming' else clause)
    return [serialize_mail(item) for item in session.scalars(query.order_by(func.coalesce(InboxMessage.sent_at, InboxMessage.created_at).desc(), InboxMessage.id.desc()).limit(500))]


@router.post("/mail/import")
async def mail_import(files: list[UploadFile] = File(...), session: Session = Depends(get_session), actor=Depends(require_staff)):
    if len(files) > 20:
        raise HTTPException(413, "За один импорт разрешено до 20 EML-файлов")
    batch, total = [], 0
    for file in files:
        raw = await file.read(MAX_MAIL_BYTES + 1)
        if len(raw) > MAX_MAIL_BYTES:
            raise HTTPException(413, "Письмо превышает лимит 5 МБ")
        total += len(raw)
        if total > 20 * 1024 * 1024:
            raise HTTPException(413, "Суммарный размер импорта превышает 20 МБ")
        batch.append((file.filename or "message.eml", raw))
    return import_eml_batch(session, batch, actor)


class MailSolutionCandidate(InputModel):
    title: str = Field(min_length=1, max_length=240)
    equipment_model: str = Field(min_length=1, max_length=120)
    symptoms: str = Field(min_length=1, max_length=20_000)
    actions: str = Field(min_length=1, max_length=20_000)
    limitations: str = Field(default='', max_length=20_000)


@router.get('/inbox/{message_id}/thread')
def mail_thread(message_id: int, offset: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=2000), session: Session = Depends(get_session)):
    message = session.scalar(select(InboxMessage).options(defer(InboxMessage.raw_mime)).where(InboxMessage.id == message_id))
    if message is None:
        raise HTTPException(404, 'Письмо не найдено')
    where = InboxMessage.case_id == message.case_id if message.case_id else InboxMessage.id == message.id
    total = session.scalar(select(func.count(InboxMessage.id)).where(where)) or 0
    records = list(session.scalars(select(InboxMessage).options(defer(InboxMessage.raw_mime)).where(where).order_by(func.coalesce(InboxMessage.sent_at, InboxMessage.created_at), InboxMessage.id).offset(offset).limit(limit)))
    case = session.get(ServiceCase, message.case_id) if message.case_id else None
    from .mail_organization import mail_metadata
    metadata = mail_metadata(message)
    return {'case_id': message.case_id, 'case_kind': case.kind if case else None,
            'customer_key': metadata['customer_key'], 'customer_label': metadata['customer_label'],
            'customer_email': metadata['customer_email'],
            'messages': [serialize_mail(item) for item in records], 'tasks': [_dict(task, 'id case_id title department assignee status created_at updated_at') for task in case.tasks] if case else [], 'total': total, 'offset': offset, 'has_more': offset + len(records) < total}


@router.post('/inbox/{message_id}/solution-candidate')
def mail_solution_candidate(message_id: int, data: MailSolutionCandidate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    from .knowledge import detect_models, normalize_model
    message = session.scalar(select(InboxMessage).options(defer(InboxMessage.raw_mime)).where(InboxMessage.id == message_id))
    if message is None:
        raise HTTPException(404, 'Письмо не найдено')
    if not message.case_id:
        raise HTTPException(422, 'Сначала привяжите письмо к сервисному обращению')
    if len(detect_models(data.equipment_model)) > 1:
        raise HTTPException(422, 'Укажите одну точную модель и модификацию оборудования')
    model = normalize_model(data.equipment_model)
    key = hashlib.sha256(f'mail-case:{message.case_id}|model:{model}'.encode()).hexdigest()
    existing = session.scalar(select(mail_models.MailSolutionOrigin).where(mail_models.MailSolutionOrigin.origin_key == key))
    if existing:
        solution = _get(session, VerifiedSolution, existing.solution_id)
        return {**serialize_solution(solution), 'existing': True, 'provenance_url': f'/api/solutions/{solution.id}/provenance'}
    records = session.execute(select(InboxMessage.id, InboxMessage.message_id, InboxMessage.content_hash, InboxMessage.source).where(InboxMessage.case_id == message.case_id).order_by(InboxMessage.id).limit(2000)).all()
    evidence = [{'id': row.id, 'message_id': row.message_id, 'sha256': row.content_hash, 'source': row.source} for row in records]
    source = f'История сервисного обращения #{message.case_id}; письмо #{message.id}; Message-ID {message.message_id}; SHA256 {message.content_hash}. Снимок происхождения сохранён отдельно; требуется проверка сотрудником.'
    solution = VerifiedSolution(title=data.title, equipment_model=model, symptoms=data.symptoms, actions=data.actions, limitations=data.limitations, source=source, customer_confirmed=False, status='CANDIDATE', visibility='internal')
    session.add(solution)
    session.flush()
    session.add(mail_models.MailSolutionOrigin(origin_key=key, solution_id=solution.id, case_id=message.case_id, inbox_id=message.id, equipment_model=model, evidence=evidence, created_by=_actor(actor)))
    record_event(session, actor, 'solution.mail_candidate_created', 'solution', solution.id, {'case_id': message.case_id, 'message_count': len(evidence), 'status': 'CANDIDATE'})
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(mail_models.MailSolutionOrigin).where(mail_models.MailSolutionOrigin.origin_key == key))
        if existing:
            return {**serialize_solution(_get(session, VerifiedSolution, existing.solution_id)), 'existing': True, 'provenance_url': f'/api/solutions/{existing.solution_id}/provenance'}
        raise HTTPException(409, 'Кандидат решения не сохранён из-за конфликта; повторите запрос') from None
    return {**serialize_solution(solution), 'existing': False, 'provenance_url': f'/api/solutions/{solution.id}/provenance'}


@router.get('/solutions/{solution_id}/provenance')
def solution_provenance(solution_id: int, session: Session = Depends(get_session)):
    _get(session, VerifiedSolution, solution_id)
    origin = session.scalar(select(mail_models.MailSolutionOrigin).where(mail_models.MailSolutionOrigin.solution_id == solution_id))
    if not origin:
        return {'solution_id': solution_id, 'mail_origin': None}
    return {'solution_id': solution_id, 'case_id': origin.case_id, 'inbox_id': origin.inbox_id, 'equipment_model': origin.equipment_model, 'evidence': origin.evidence, 'created_by': origin.created_by, 'created_at': _iso(origin.created_at)}


class MailSyncRequest(InputModel):
    kind: Literal['new', 'history'] = 'new'
    days: int = Field(default=365, ge=1, le=3650)
    batch_size: int = Field(default=100, ge=1, le=500)


class MailWorkerRequest(InputModel):
    enabled: bool
    poll_seconds: int = Field(default=60, ge=30, le=3600)


def _mail_config():
    from .mail_sync import mail_config
    return mail_config()


@router.get('/mail/status')
def mail_status(session: Session = Depends(get_session)):
    from .mail_sync import get_mail_status
    return get_mail_status(session)


@router.post('/mail/check')
def mail_check(session: Session = Depends(get_session), actor=Depends(require_admin)):
    from .mail_sync import check_connection
    return check_connection(session, actor)


@router.post('/mail/sync', status_code=202)
def mail_sync_now(data: MailSyncRequest, session: Session = Depends(get_session), actor=Depends(require_admin)):
    from .mail_sync import queue_sync
    return queue_sync(session, kind=data.kind, days=data.days, batch_size=data.batch_size, actor=actor)


@router.post('/mail/history', status_code=202)
def mail_history_import(data: MailSyncRequest, session: Session = Depends(get_session), actor=Depends(require_admin)):
    from .mail_sync import queue_sync
    return queue_sync(session, kind='history', days=data.days, batch_size=data.batch_size, actor=actor)


@router.get('/mail/runs')
def mail_runs(session: Session = Depends(get_session)):
    from .mail_sync import account_key, run_dict
    return [run_dict(run) for run in session.scalars(select(mail_models.MailSyncRun).where(mail_models.MailSyncRun.account_key == account_key()).order_by(mail_models.MailSyncRun.id.desc()).limit(100))]


@router.post('/mail/runs/{run_id}/stop')
def stop_mail_run(run_id: int, session: Session = Depends(get_session), actor=Depends(require_admin)):
    from .mail_sync import account_key, run_dict
    run = _get(session, mail_models.MailSyncRun, run_id)
    if run.account_key != account_key():
        raise HTTPException(404, 'Задача почты не найдена')
    run.stop_requested = True
    if run.status == 'queued':
        run.status, run.finished_at = 'stopped', utcnow()
    record_event(session, actor, 'mail.stop_requested', 'mail_run', run.id)
    session.commit()
    return run_dict(run)


@router.patch('/mail/worker')
def configure_mail_worker(data: MailWorkerRequest, session: Session = Depends(get_session), actor=Depends(require_admin)):
    from .mail_sync import configure_worker
    return configure_worker(session, data.enabled, data.poll_seconds, actor)


@router.get('/mail/attachments/{attachment_id}/download')
def download_mail_attachment(attachment_id: int, session: Session = Depends(get_session)):
    from pathlib import Path
    item = _get(session, mail_models.MailAttachment, attachment_id)
    if item.status != 'available' or not item.storage_path:
        raise HTTPException(403, 'Этот тип вложения заблокирован для безопасной обработки')
    root = (settings.storage_dir / 'mail_attachments').resolve()
    path = Path(item.storage_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        # A private storage directory may have moved during a local update.
        # Only locate the generated basename inside the current attachment root;
        # verify its size and digest below before returning any bytes.
        relocated = root / str(item.storage_path).replace('\\', '/').rsplit('/', 1)[-1]
        if relocated.is_file():
            path = relocated.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        from .mail_attachment_cache import restore_missing_attachment
        restored = restore_missing_attachment(session, item)
        if restored is not None:
            path = restored.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, 'Вложение и его исходное содержимое не найдены. Восстановите приватное хранилище из резервной копии.')
    if path.stat().st_size != item.size or hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
        raise HTTPException(409, 'Нарушена целостность вложения; повторите импорт исходного письма')
    return FileResponse(path, filename=item.filename, media_type='application/octet-stream', headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@router.post("/inbox/{message_id}/draft", status_code=201)
def create_draft(message_id: int, data: DraftCreate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    message = _get(session, InboxMessage, message_id)
    from .mail_ai import generate_draft, reply_target
    recipient = data.recipient or reply_target(session, message)
    if not recipient:
        raise HTTPException(422, 'В цепочке не найден однозначный адрес клиента. Укажите получателя вручную; собственный ящик не выбирается автоматически.')
    if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", recipient):
        raise HTTPException(422, "Укажите один корректный адрес получателя")
    body = data.body
    generation = None
    if not data.body:
        result = generate_draft(session, message, recipient=recipient, reply_goal=data.reply_goal)
        if result.get('engine') != 'openai' or not result.get('content', '').strip():
            raise HTTPException(503, {'code': 'ai_draft_unavailable', 'message': result.get('ai_error') or 'ИИ не подготовил полезный почтовый черновик. Ответ можно написать вручную.'})
        body = result["content"]
        sources = result.get("sources") or []
        if sources:
            body += "\n\nИсточники:\n" + "\n".join(f"[{index}] {source.get('title', 'Источник')}" + (f", стр. {source['page']}" if source.get("page") else "") for index, source in enumerate(sources, 1))
        generation = {"engine": "openai", "source_count": len(sources), 'intent': result.get('intent'),
                      'missing_information': result.get('missing_information', []), 'trace': result.get('trace', []),
                      'model': result.get('model'), 'usage': result.get('usage')}
    subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
    item = Approval(inbox_id=message.id, case_id=message.case_id, subject=subject[:998], recipient=recipient, body=body, created_by=_actor(actor))
    session.add(item)
    session.flush()
    record_event(session, actor, "draft.created", "approval", item.id, {"case_id": item.case_id, "generated": generation is not None})
    session.commit()
    result = serialize_approval(item)
    if generation:
        result["generation"] = generation
    return result


@router.get("/approvals")
def approvals(session: Session = Depends(get_session)):
    return [serialize_approval(item) for item in session.scalars(select(Approval).order_by(Approval.id.desc()).limit(500))]


@router.patch("/approvals/{approval_id}")
def update_approval(approval_id: int, data: ApprovalUpdate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = _get(session, Approval, approval_id)
    if data.recipient is not None and not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", data.recipient):
        raise HTTPException(422, "Укажите один корректный адрес получателя")
    if data.recipient is not None and data.recipient != item.recipient:
        item.recipient = data.recipient
        item.status = "PENDING"
        item.approved_by = None
    if data.body is not None and data.body != item.body:
        item.body = data.body
        item.status = "PENDING"
        item.approved_by = None
    if data.status is not None:
        item.status = data.status
        item.approved_by = _actor(actor) if data.status == "APPROVED" else None
    record_event(session, actor, "draft.updated", "approval", item.id, {"status": item.status, "edited": data.body is not None or data.recipient is not None})
    session.commit()
    return serialize_approval(item)


@router.post("/approvals/{approval_id}/send")
def send_approval(approval_id: int, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = _get(session, Approval, approval_id)
    record_event(session, actor, "mail.send_blocked", "approval", item.id, {"status": item.status})
    session.commit()
    raise HTTPException(409, {"code": "sending_disabled", "message": "Отправка писем отключена в этой версии. Согласование сохраняет локальный черновик и ничего не отправляет."})


def normalize_serial(value):
    # Deliberately no letter substitution or fuzzy matching: those can merge machines.
    return re.sub(r"[\s\-‐‑‒–—−]+", "", unicodedata.normalize("NFC", str(value))).strip()


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_equipment_workbook(session: Session, raw: bytes, actor):
    from openpyxl import load_workbook
    if len(raw) > MAX_WORKBOOK_BYTES:
        raise HTTPException(413, "Реестр превышает лимит 15 МБ")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 120 * 1024 * 1024:
                raise HTTPException(413, "Распакованный XLSX превышает 120 МБ")
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    except HTTPException:
        raise
    except (BadZipFile, ValueError, OSError, KeyError):
        raise HTTPException(422, "Некорректный файл XLSX") from None
    imported = skipped = duplicates = ambiguous = enriched = 0
    issues = []
    digest = hashlib.sha256(raw).hexdigest()
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True, max_col=12)
        header = next(rows, ())
        expected = {1: "Серийный номер", 4: "Проект", 6: "Модель", 7: "Наименование", 8: "Дата отгрузки"}
        if any(len(header) <= index or _cell(header[index]).casefold() != name.casefold() for index, name in expected.items()):
            raise HTTPException(422, "Не распознаны заголовки реестра: Серийный номер, Проект, Модель, Наименование, Дата отгрузки")
        existing = {item.normalized_serial: item for item in session.scalars(select(Equipment))}
        for number, row in enumerate(rows, 2):
            if number > 50_001:
                raise HTTPException(413, "В одном реестре разрешено до 50 000 строк")
            values = list(row) + [None] * max(0, 12 - len(row))
            serial = _cell(values[1])
            if not serial:
                skipped += 1
                continue
            if serial.startswith(("=", "+", "@")) or len(serial) > 240:
                ambiguous += 1
                if len(issues) < 100:
                    issues.append({"row": number, "serial": serial[:240], "reason": "invalid_serial"})
                continue
            key = normalize_serial(serial)
            if not key:
                ambiguous += 1
                if len(issues) < 100:
                    issues.append({"row": number, "serial": serial[:240], "reason": "empty_normalized_serial"})
                continue
            shipped = values[8]
            fields = {"serial_number": serial, "normalized_serial": key, "project": _cell(values[4])[:240], "model": _cell(values[6])[:240], "name": _cell(values[7])[:500], "shipped_at": shipped.isoformat() if isinstance(shipped, (datetime, date)) else _cell(shipped)[:80] or None, "customer": _cell(values[11])[:240] or None}
            if key in existing:
                prior = existing[key]
                changed_fields = [field for field, value in fields.items() if field not in ("serial_number", "normalized_serial") and getattr(prior, field) != value]
                if changed_fields == ["customer"] and prior.customer is None and fields["customer"]:
                    prior.customer = fields["customer"]
                    enriched += 1
                elif changed_fields:
                    ambiguous += 1
                    if len(issues) < 100:
                        issues.append({"row": number, "serial": serial, "reason": "serial_conflict: " + ", ".join(changed_fields)})
                else:
                    duplicates += 1
                continue
            item = Equipment(**fields)
            session.add(item)
            existing[key] = item
            imported += 1
        archive_dir = settings.storage_dir / "registry_imports"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{digest}.xlsx"
        if not archive_path.exists():
            archive_path.write_bytes(raw)
        record_event(session, actor, "equipment.imported", "equipment", "workbook", {"imported": imported, "skipped": skipped, "duplicates": duplicates, "ambiguous": ambiguous, "enriched": enriched, "source_sha256": digest, "issues": issues})
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        workbook.close()
    return {"imported": imported, "skipped": skipped, "duplicates": duplicates, "ambiguous": ambiguous, "enriched": enriched, "issues": issues, "source_sha256": digest, "warranty_status": "unknown"}


@router.get("/equipment")
def equipment(q: str = "", session: Session = Depends(get_session)):
    query = select(Equipment)
    if q.strip():
        text = q.strip()[:200]
        query = query.where(or_(Equipment.normalized_serial.contains(normalize_serial(text), autoescape=True), Equipment.model.contains(text, autoescape=True), Equipment.project.contains(text, autoescape=True), Equipment.name.contains(text, autoescape=True)))
    return [serialize_equipment(item) for item in session.scalars(query.order_by(Equipment.id).limit(500))]


@router.post("/equipment/import")
async def equipment_import(file: UploadFile = File(...), session: Session = Depends(get_session), actor=Depends(require_staff)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(422, "Реестр должен быть файлом .xlsx")
    raw = await file.read(MAX_WORKBOOK_BYTES + 1)
    return import_equipment_workbook(session, raw, actor)


@router.get("/solutions")
def solutions(session: Session = Depends(get_session)):
    return [serialize_solution(item) for item in session.scalars(select(VerifiedSolution).order_by(VerifiedSolution.id.desc()).limit(500))]


@router.post("/solutions", status_code=201)
def create_solution(data: SolutionCreate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = VerifiedSolution(**data.model_dump())
    session.add(item)
    session.flush()
    record_event(session, actor, "solution.created", "solution", item.id, {"status": item.status})
    session.commit()
    return serialize_solution(item)


@router.patch("/solutions/{solution_id}")
def update_solution(solution_id: int, data: SolutionUpdate, session: Session = Depends(get_session), actor=Depends(require_staff)):
    item = _get(session, VerifiedSolution, solution_id)
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "Поле не может быть пустым")
    target_status = changes.get("status", item.status)
    substantive_edits = any(key in changes and changes[key] != getattr(item, key) for key in ("title", "equipment_model", "symptoms", "actions", "limitations", "source", "visibility"))
    if item.status == "VERIFIED" and substantive_edits and "status" not in changes:
        target_status = "NEEDS_REVIEW"
    if target_status == "VERIFIED":
        if getattr(actor, "role", "") not in ("admin", "operator", "engineer"):
            raise HTTPException(403, "Подтвердить решение может уполномоченный сотрудник")
        if not changes.get("actions", item.actions).strip() or not changes.get("source", item.source).strip():
            raise HTTPException(422, "Для подтверждения нужны выполненные действия и проверяемый первоисточник")
    for key, value in changes.items():
        setattr(item, key, value)
    item.status = target_status
    item.verified_by = _actor(actor) if target_status == "VERIFIED" else None
    record_event(session, actor, "solution.updated", "solution", item.id, {"status": item.status, "fields": list(changes)})
    session.commit()
    return serialize_solution(item)


def search_verified_solutions(session: Session, q: str, public_only: bool = True):
    """Return approved sources only; internal solutions require explicit staff use."""
    from server.knowledge import detect_models
    def terms_for(value):
        words = re.findall(r"[a-zа-я0-9-]{2,}", value.casefold().replace("ё", "е"))
        return [re.sub(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ение|ения|ений|ость|ости|ать|ять|ить|ться|ется|ются|ов|ев|ий|ый|ой|ая|яя|ое|ее|ые|ие|ам|ям|ах|ях|ом|ем|ую|юю|ы|и|а|я|у|ю|е|о)$", "", word) if len(word) > 4 and re.search("[а-я]", word) else word for word in words]
    terms = list(dict.fromkeys(terms_for(q)))[:16]
    required_models = set(detect_models(q))
    if not terms:
        return []
    query = select(VerifiedSolution).where(VerifiedSolution.status == "VERIFIED")
    if public_only:
        query = query.where(VerifiedSolution.visibility == "public")
    candidates = list(session.scalars(query.limit(2000)))
    scored = []
    for item in candidates:
        item_models = set(detect_models(item.equipment_model or item.title))
        if required_models and not required_models.intersection(item_models):
            continue
        haystack = terms_for(" ".join((item.title, item.equipment_model, item.symptoms, item.actions)))
        score = sum(haystack.count(term) for term in terms)
        if score:
            scored.append((score, item.id, item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [serialize_solution(item) for _, _, item in scored[:8]]


@router.get("/audit")
def audit(session: Session = Depends(get_session)):
    return [_dict(item, "id actor action entity_type entity_id detail created_at") for item in session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200))]


@router.get("/departments")
def departments(session: Session = Depends(get_session)):
    return [_dict(item, "id name responsibility email backup_assignee active") for item in session.scalars(select(Department).order_by(Department.name))]


@router.post("/departments", status_code=201)
def create_department(data: DepartmentCreate, session: Session = Depends(get_session), actor=Depends(require_admin)):
    if session.scalar(select(Department.id).where(Department.name == data.name)):
        raise HTTPException(409, "Подразделение с таким названием уже существует")
    item = Department(**data.model_dump())
    session.add(item)
    session.flush()
    record_event(session, actor, "department.created", "department", item.id)
    session.commit()
    return _dict(item, "id name responsibility email backup_assignee active")
