"""Persistent, private IMAP checkpoints and jobs. No SMTP credentials or secrets."""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from .db import Base, utcnow


class MailFolderState(Base):
    __tablename__ = 'mail_folder_states'
    __table_args__ = (UniqueConstraint('account_key', 'folder', name='uq_mail_folder_account'),)
    id = Column(Integer, primary_key=True)
    account_key = Column(String(64), nullable=False, index=True)
    folder = Column(String(512), nullable=False)
    kind = Column(String(16), nullable=False)
    uidvalidity = Column(String(80), nullable=True)
    last_uid = Column(BigInteger, nullable=False, default=0)
    new_initialized = Column(Boolean, nullable=False, default=False)
    initial_upper_uid = Column(BigInteger, nullable=True)
    initial_since = Column(String(10), nullable=True)
    history_last_uid = Column(BigInteger, nullable=False, default=0)
    history_since = Column(String(10), nullable=True)
    history_days = Column(Integer, nullable=True)
    history_completed = Column(Boolean, nullable=False, default=False)
    last_sync = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)


class MailRemoteMessage(Base):
    __tablename__ = 'mail_remote_messages'
    __table_args__ = (UniqueConstraint('account_key', 'folder', 'uidvalidity', 'uid', name='uq_mail_remote_uid'),)
    id = Column(Integer, primary_key=True)
    account_key = Column(String(64), nullable=False, index=True)
    folder = Column(String(512), nullable=False)
    uidvalidity = Column(String(80), nullable=False)
    uid = Column(BigInteger, nullable=False)
    inbox_id = Column(Integer, ForeignKey('inbox_messages.id'), nullable=True, index=True)
    status = Column(String(24), nullable=False)
    direction = Column(String(16), nullable=False, default='incoming')
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    inbox = relationship('InboxMessage', backref='mail_locations')


class MailAttachment(Base):
    __tablename__ = 'mail_attachments'
    __table_args__ = (UniqueConstraint('inbox_id', 'sha256', 'filename', name='uq_mail_attachment'),)
    id = Column(Integer, primary_key=True)
    inbox_id = Column(Integer, ForeignKey('inbox_messages.id'), nullable=False, index=True)
    filename = Column(String(240), nullable=False)
    content_type = Column(String(160), nullable=False)
    size = Column(BigInteger, nullable=False)
    sha256 = Column(String(64), nullable=False)
    storage_path = Column(Text, nullable=True)
    status = Column(String(24), nullable=False)
    reason = Column(String(240), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class MailSyncRun(Base):
    __tablename__ = 'mail_sync_runs'
    id = Column(Integer, primary_key=True)
    account_key = Column(String(64), nullable=False, index=True)
    kind = Column(String(16), nullable=False, default='new')
    status = Column(String(24), nullable=False, default='queued', index=True)
    days = Column(Integer, nullable=False, default=365)
    batch_size = Column(Integer, nullable=False, default=100)
    imported = Column(Integer, nullable=False, default=0)
    duplicates = Column(Integer, nullable=False, default=0)
    fetched = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    progress = Column(JSON, nullable=False, default=list)
    has_more = Column(Boolean, nullable=False, default=False)
    stop_requested = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(80), nullable=True)
    error = Column(Text, nullable=True)
    actor = Column(String(160), nullable=False, default='worker')
    created_at = Column(DateTime, nullable=False, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class MailSyncControl(Base):
    __tablename__ = 'mail_sync_controls'
    account_key = Column(String(64), primary_key=True)
    background_enabled = Column(Boolean, nullable=False, default=False)
    poll_seconds = Column(Integer, nullable=False, default=60)
    lease_owner = Column(String(80), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)
    last_connection = Column(DateTime, nullable=True)
    connection_error = Column(String(80), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class MailSolutionOrigin(Base):
    __tablename__ = 'mail_solution_origins'
    id = Column(Integer, primary_key=True)
    origin_key = Column(String(64), nullable=False, unique=True)
    solution_id = Column(Integer, ForeignKey('verified_solutions.id'), nullable=False, unique=True)
    case_id = Column(Integer, ForeignKey('service_cases.id'), nullable=False)
    inbox_id = Column(Integer, ForeignKey('inbox_messages.id'), nullable=False)
    equipment_model = Column(String(120), nullable=False)
    evidence = Column(JSON, nullable=False, default=list)
    created_by = Column(String(160), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
