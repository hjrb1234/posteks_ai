"""IMAP integration tests use a protocol mock and an isolated local database.

No credentials, external connections, SMTP calls, or source mailboxes are used.
The mock records IMAP commands so read-only behavior is an assertion, not a label.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import imaplib
from pathlib import Path
import re
import time
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server import workflows
from server.auth import require_staff
from server.config import settings
from server.db import Base, get_session


def make_eml(identifier: str, *, references: str = "", subject: str = "TEST: обращение", attachments=False, sent_at=None):
    message = EmailMessage()
    message["From"] = "Тестовый клиент <client@tests.invalid>"
    message["To"] = "service@tests.invalid"
    message["Subject"] = subject
    message["Message-ID"] = identifier
    message["Date"] = format_datetime(sent_at or datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc))
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    message.set_content("Тестовое письмо. Спасибо, проблему удалось исправить.")
    if attachments:
        message.add_attachment(b"%PDF-1.7\n% isolated test attachment\n%%EOF\n", maintype="application", subtype="pdf", filename="../../руководство.pdf")
        message.add_attachment(b"MZ-do-not-execute-test", maintype="application", subtype="octet-stream", filename="payload.exe")
    return message.as_bytes()


class MockIMAPServer:
    """Small UID-capable IMAP source, including read-only folders and reset epochs."""

    def __init__(self, inbox=None, sent=None):
        self.folders = {"INBOX": dict(inbox or {}), "Sent": dict(sent or {})}
        self.validity = {"INBOX": 1042, "Sent": 2042}
        self.calls = []
        self.size_overrides = {}
        self.fail_once = set()
        self.connections = 0
        self.username = "service@tests.invalid"
        self.internal_dates = {}
        self.on_fetch = None

    def factory(self, *args, **kwargs):
        self.connections += 1
        assert kwargs.get("ssl_context") is None or kwargs["ssl_context"].check_hostname
        return MockIMAPConnection(self)

    def fetched_uids(self, folder="INBOX", *, body_only=True):
        return [call[2] for call in self.calls if call[0] == "fetch" and call[1] == folder and (not body_only or "BODY.PEEK" in call[3])]


class MockIMAPConnection:
    capabilities = (b"IMAP4REV1", b"UIDPLUS", b"SPECIAL-USE")

    def __init__(self, server):
        self.server = server
        self.folder = None
        self.untagged_responses = {}

    def login(self, username, password):
        if username != self.server.username or password != "fake-imap-password":
            pytest.fail("Mock received an unexpected credential binding")
        self.server.calls.append(("login",))
        return "OK", [b"authenticated"]

    def list(self, *args):
        self.server.calls.append(("list",))
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, folder="INBOX", readonly=False):
        if isinstance(folder, bytes):
            folder = folder.decode()
        folder = str(folder).strip('"')
        assert readonly is True, "IMAP folder selection must be read-only"
        self.server.calls.append(("select", folder, readonly))
        if folder not in self.server.folders:
            return "NO", [b"mailbox not found"]
        self.folder = folder
        self.untagged_responses["UIDVALIDITY"] = [str(self.server.validity[folder]).encode()]
        self.untagged_responses["UIDNEXT"] = [str(max(self.server.folders[folder], default=0) + 1).encode()]
        return "OK", [str(len(self.server.folders[folder])).encode()]

    def response(self, name):
        if str(name).upper() == "UIDNEXT":
            return "UIDNEXT", [str(max(self.server.folders[self.folder], default=0) + 1).encode()]
        assert str(name).upper() == "UIDVALIDITY"
        return "UIDVALIDITY", [str(self.server.validity[self.folder]).encode()]

    def status(self, folder, attributes):
        folder = str(folder).strip('"')
        validity = self.server.validity[folder]
        maximum = max(self.server.folders[folder], default=0)
        return "OK", [f'{folder} (UIDVALIDITY {validity} UIDNEXT {maximum + 1} MESSAGES {len(self.server.folders[folder])})'.encode()]

    def uid(self, command, *args):
        command = command.lower()
        if command == "search":
            criteria = " ".join(str(value) for value in args if value is not None)
            self.server.calls.append(("search", self.folder, criteria))
            uids = sorted(self.server.folders[self.folder])
            match = re.search(r"\bUID\s+(\d+):(\d+|\*)", criteria, re.I)
            if match:
                # RFC 3501 may include the last UID for an inverted range. The
                # service must still discard already processed UIDs itself.
                minimum = int(match.group(1))
                uids = [uid for uid in uids if uid >= minimum]
                if match.group(2) != "*":
                    uids = [uid for uid in uids if uid <= int(match.group(2))]
            since = re.search(r'SINCE\s+"?(\d{1,2}-[A-Za-z]{3}-\d{4})', criteria, re.I)
            if since:
                cutoff = datetime.strptime(since.group(1), "%d-%b-%Y").date()
                uids = [uid for uid in uids if self.server.internal_dates.get((self.folder, uid), datetime.now(timezone.utc).date()) >= cutoff]
            return "OK", [" ".join(map(str, uids)).encode()]
        assert command == "fetch", f"Unexpected IMAP mutation/operation: {command}"
        uid = int(args[0])
        query = str(args[1]).upper()
        self.server.calls.append(("fetch", self.folder, uid, query))
        key = (self.folder, uid)
        if key in self.server.fail_once:
            self.server.fail_once.remove(key)
            raise OSError("simulated temporary connection loss")
        raw = self.server.folders[self.folder][uid]
        size = self.server.size_overrides.get(key, len(raw))
        if "BODY.PEEK" in query:
            assert key not in self.server.size_overrides, "Oversized bodies must not be downloaded"
            if self.server.on_fetch:
                self.server.on_fetch(self.folder, uid)
            return "OK", [(f"{uid} (UID {uid} RFC822.SIZE {size} BODY[] {{{size}}}".encode(), raw), b")"]
        assert "RFC822.SIZE" in query
        return "OK", [f"{uid} (UID {uid} RFC822.SIZE {size})".encode()]

    def logout(self):
        self.server.calls.append(("logout",))
        return "BYE", [b"logged out"]

    def close(self):
        self.server.calls.append(("close", self.folder))
        return "OK", [b"closed"]

    def store(self, *args):
        pytest.fail("Sync must never change read/unread flags")

    def expunge(self, *args):
        pytest.fail("Sync must never delete messages")


@pytest.fixture
def mail_app(tmp_path, monkeypatch):
    from server import mail_models, mail_sync
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "app_mode", "SAFE")
    for name in ("IMAP_HOST", "IMAP_USER", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_PORT", "MAIL_SENT_FOLDER", "MAIL_INBOX_FOLDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.tests.invalid")
    monkeypatch.setenv("MAIL_USERNAME", "service@tests.invalid")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "fake-imap-password")
    monkeypatch.setenv("MAIL_MAILBOX", "INBOX")
    monkeypatch.setenv("MAIL_POLL_ENABLED", "false")
    monkeypatch.setenv("MAIL_POLL_SECONDS", "60")
    monkeypatch.setenv("MAIL_INITIAL_DAYS", "7")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(mail_sync, "SessionLocal", factory)
    app = FastAPI()
    app.include_router(workflows.router)
    actor = SimpleNamespace(id=1, username="mail-test-admin", role="admin")
    def sessions():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[require_staff] = lambda: actor
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, factory=factory, actor=actor, sync=mail_sync, models=mail_models)
    engine.dispose()


def run_sync(context, server, **kwargs):
    with context.factory() as session:
        return context.sync.sync_mailbox(session, actor=context.actor, imap_factory=server.factory, **kwargs)


def inbox_rows(context):
    with context.factory() as session:
        return list(session.scalars(select(workflows.InboxMessage).order_by(workflows.InboxMessage.id)))


def test_uid_cursor_survives_new_session_and_uses_read_only_commands(mail_app):
    server = MockIMAPServer({1: make_eml("<one@tests.invalid>"), 2: make_eml("<two@tests.invalid>")})
    run_sync(mail_app, server)
    assert len(inbox_rows(mail_app)) == 2
    assert server.fetched_uids() == [1, 2]
    server.calls.clear()
    # A new Session reproduces a worker restart: no process-local cursor remains.
    run_sync(mail_app, server)
    assert len(inbox_rows(mail_app)) == 2 and server.fetched_uids() == []
    server.folders["INBOX"][3] = make_eml("<three@tests.invalid>")
    run_sync(mail_app, server)
    assert len(inbox_rows(mail_app)) == 3 and server.fetched_uids() == [3]
    assert all(call[2] is True for call in server.calls if call[0] == "select")
    assert not any(call[0] in {"store", "expunge", "copy", "move"} for call in server.calls)


def test_inbox_sent_dedup_and_references_threading_not_subject(mail_app):
    root = make_eml("<root@tests.invalid>")
    reply = make_eml("<reply@tests.invalid>", references="<root@tests.invalid>", subject="Re: изменённая тема")
    unrelated = make_eml("<other@tests.invalid>")
    server = MockIMAPServer({2: root, 3: unrelated}, {20: reply, 21: root})
    run_sync(mail_app, server)
    rows = {row.message_id: row for row in inbox_rows(mail_app)}
    assert len(rows) == 3
    assert rows["<root@tests.invalid>"].case_id == rows["<reply@tests.invalid>"].case_id
    assert rows["<root@tests.invalid>"].case_id != rows["<other@tests.invalid>"].case_id
    assert any(call[:2] == ("select", "Sent") for call in server.calls)
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.VerifiedSolution)) == 0


def test_uidvalidity_reset_reprocesses_without_losing_new_mail_or_duplicating(mail_app):
    server = MockIMAPServer({91: make_eml("<persisted@tests.invalid>")})
    run_sync(mail_app, server)
    server.validity["INBOX"] += 1
    server.folders["INBOX"] = {1: make_eml("<persisted@tests.invalid>"), 2: make_eml("<after-reset@tests.invalid>")}
    server.calls.clear()
    run_sync(mail_app, server)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<persisted@tests.invalid>", "<after-reset@tests.invalid>"}
    assert server.fetched_uids() == [1, 2]


def test_batch_progress_and_history_are_persisted_without_automatic_knowledge(mail_app):
    server = MockIMAPServer({uid: make_eml(f"<history-{uid}@tests.invalid>") for uid in (1, 2, 3)})
    run_sync(mail_app, server, kind="history", days=365, batch_size=1)
    assert len(inbox_rows(mail_app)) == 1
    run_sync(mail_app, server, kind="history", days=365, batch_size=1)
    assert len(inbox_rows(mail_app)) == 2
    run_sync(mail_app, server, kind="history", days=365, batch_size=1)
    assert len(inbox_rows(mail_app)) == 3
    searches = [call[2] for call in server.calls if call[0] == "search"]
    assert any("SINCE" in criteria for criteria in searches)
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.VerifiedSolution)) == 0
        assert session.scalar(select(func.count()).select_from(mail_app.models.MailSyncRun)) >= 3


def test_connection_failure_retries_failed_uid_instead_of_advancing_past_it(mail_app):
    server = MockIMAPServer({1: make_eml("<before-failure@tests.invalid>"), 2: make_eml("<retry@tests.invalid>"), 3: make_eml("<after-failure@tests.invalid>")})
    server.fail_once.add(("INBOX", 2))
    failed = run_sync(mail_app, server)
    assert failed["status"] == "failed" and failed["error_code"]
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<before-failure@tests.invalid>"}
    run_sync(mail_app, server)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<before-failure@tests.invalid>", "<retry@tests.invalid>", "<after-failure@tests.invalid>"}


def test_test_mode_blocks_real_connection_even_with_configured_credentials(mail_app, monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "TEST")
    def forbidden(*args, **kwargs):
        pytest.fail("TEST mode must never open a real IMAP socket")
    for operation in (mail_app.sync.sync_mailbox, mail_app.sync.check_connection):
        with mail_app.factory() as session:
            try:
                operation(session, actor=mail_app.actor, imap_factory=forbidden)
            except HTTPException as error:
                assert error.status_code == 409
    assert inbox_rows(mail_app) == []


def test_oversize_and_malformed_messages_do_not_block_later_valid_mail(mail_app):
    server = MockIMAPServer({1: b"oversized", 2: b"not a MIME message", 3: make_eml("<valid-after-bad@tests.invalid>")})
    server.size_overrides[("INBOX", 1)] = 100 * 1024 * 1024
    result = run_sync(mail_app, server)
    assert result["errors"] >= 2
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<valid-after-bad@tests.invalid>"}
    assert 1 not in server.fetched_uids()
    with mail_app.factory() as session:
        status = mail_app.sync.get_mail_status(session)
    assert status["sending_enabled"] is False
    assert "fake-imap-password" not in str(status)


def test_attachments_saved_privately_downloadable_to_staff_and_executables_blocked(mail_app):
    server = MockIMAPServer({1: make_eml("<attachments@tests.invalid>", attachments=True)})
    run_sync(mail_app, server)
    with mail_app.factory() as session:
        attachments = list(session.scalars(select(mail_app.models.MailAttachment)))
    available = [item for item in attachments if item.status == "available"]
    blocked = [item for item in attachments if item.status == "blocked"]
    assert len(available) == len(blocked) == 1
    assert available[0].filename == "руководство.pdf" and blocked[0].filename == "payload.exe"
    storage_path = Path(available[0].storage_path)
    if not storage_path.is_absolute():
        storage_path = settings.storage_dir / storage_path
    assert storage_path.resolve().is_relative_to(settings.storage_dir.resolve())
    assert storage_path.read_bytes().startswith(b"%PDF-")
    response = mail_app.client.get(f"/api/mail/attachments/{available[0].id}/download")
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert "attachment" in response.headers.get("content-disposition", "")
    assert mail_app.client.get(f"/api/mail/attachments/{blocked[0].id}/download").status_code == 403
    inbox_response = mail_app.client.get("/api/inbox")
    assert str(settings.storage_dir) not in inbox_response.text
    del mail_app.app.dependency_overrides[require_staff]
    assert mail_app.client.get(f"/api/mail/attachments/{available[0].id}/download").status_code == 401


def test_document_extension_cannot_disguise_an_executable_attachment(mail_app):
    message = EmailMessage()
    message["From"] = "client@tests.invalid"
    message["To"] = "service@tests.invalid"
    message["Subject"] = "TEST attachment validation"
    message["Message-ID"] = "<disguised@tests.invalid>"
    message.set_content("Проверка содержимого вложения")
    message.add_attachment(b"MZ-executable-test-only", maintype="application", subtype="pdf", filename="document.pdf")
    server = MockIMAPServer({1: message.as_bytes()})
    run_sync(mail_app, server)
    with mail_app.factory() as session:
        attachment = session.scalar(select(mail_app.models.MailAttachment))
    assert attachment is not None and attachment.status == "blocked" and attachment.reason


def test_persisted_test_mode_overrides_static_safe_mode(mail_app):
    from server.models import AppSetting
    with mail_app.factory() as session:
        session.add(AppSetting(key="mode", value={"mode": "TEST"}))
        session.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("Persisted TEST mode must block read-only integration too")
    with mail_app.factory() as session:
        with pytest.raises(HTTPException) as caught:
            mail_app.sync.sync_mailbox(session, actor=mail_app.actor, imap_factory=forbidden)
    assert caught.value.status_code == 409


def test_new_account_does_not_reuse_another_accounts_checkpoint(mail_app, monkeypatch):
    first = MockIMAPServer({50: make_eml("<first-account@tests.invalid>")})
    run_sync(mail_app, first)
    monkeypatch.setenv("MAIL_USERNAME", "other@tests.invalid")
    second = MockIMAPServer({1: make_eml("<second-account@tests.invalid>")})
    second.username = "other@tests.invalid"
    run_sync(mail_app, second)
    assert second.fetched_uids() == [1]
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<first-account@tests.invalid>", "<second-account@tests.invalid>"}
    with mail_app.factory() as session:
        states = list(session.scalars(select(mail_app.models.MailFolderState)))
        assert len({state.account_key for state in states}) == 2
        assert all("@" not in state.account_key for state in states)


def test_authentication_failure_is_diagnostic_redacted_and_persistent(mail_app):
    class DeniedIMAP:
        def login(self, *args):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED fake-imap-password service@tests.invalid")
        def logout(self):
            return "BYE", []
    with mail_app.factory() as session:
        try:
            mail_app.sync.check_connection(session, actor=mail_app.actor, imap_factory=lambda *a, **kw: DeniedIMAP())
        except HTTPException as error:
            assert error.status_code == 502
    with mail_app.factory() as session:
        status = mail_app.sync.get_mail_status(session)
    rendered = str(status)
    assert "fake-imap-password" not in rendered and "service@tests.invalid" not in rendered
    assert status.get("error") or status.get("connection_error") or status.get("last_error")


def test_initial_new_sync_leaves_old_history_for_explicit_history_import(mail_app):
    server = MockIMAPServer({1: make_eml("<older-mail@tests.invalid>"), 2: make_eml("<recent-mail@tests.invalid>")})
    server.internal_dates[("INBOX", 1)] = (datetime.now(timezone.utc) - timedelta(days=90)).date()
    run_sync(mail_app, server, kind="new")
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<recent-mail@tests.invalid>"}
    server.calls.clear()
    run_sync(mail_app, server, kind="new")
    assert server.fetched_uids() == []
    run_sync(mail_app, server, kind="history", days=365)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<older-mail@tests.invalid>", "<recent-mail@tests.invalid>"}


def test_initial_snapshot_does_not_skip_mail_arriving_during_import(mail_app):
    server = MockIMAPServer({1: make_eml("<snapshot-one@tests.invalid>"), 2: make_eml("<snapshot-two@tests.invalid>")})
    def arrival(folder, uid):
        if folder == "INBOX" and uid == 1:
            server.folders["INBOX"][3] = make_eml("<arrived-during-sync@tests.invalid>")
    server.on_fetch = arrival
    run_sync(mail_app, server)
    run_sync(mail_app, server)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<snapshot-one@tests.invalid>", "<snapshot-two@tests.invalid>", "<arrived-during-sync@tests.invalid>"}


def test_reply_imported_before_root_is_recovered_when_root_arrives(mail_app):
    server = MockIMAPServer({1: make_eml("<early-reply@tests.invalid>", references="<late-root@tests.invalid>")})
    run_sync(mail_app, server)
    server.folders["INBOX"][2] = make_eml("<late-root@tests.invalid>")
    run_sync(mail_app, server)
    rows = {row.message_id: row for row in inbox_rows(mail_app)}
    assert rows["<late-root@tests.invalid>"].case_id is not None
    assert rows["<late-root@tests.invalid>"].case_id == rows["<early-reply@tests.invalid>"].case_id


def test_conflicting_references_do_not_merge_independent_service_cases(mail_app):
    server = MockIMAPServer({1: make_eml("<independent-a@tests.invalid>"), 2: make_eml("<independent-b@tests.invalid>"), 3: make_eml("<conflicting@tests.invalid>", references="<independent-a@tests.invalid> <independent-b@tests.invalid>")})
    run_sync(mail_app, server)
    rows = {row.message_id: row for row in inbox_rows(mail_app)}
    case_ids = {rows[key].case_id for key in rows}
    assert None not in case_ids and len(case_ids) == 3


@pytest.mark.parametrize("recover_stale_running_job", [False, True])
def test_background_worker_consumes_a_persisted_manual_job(mail_app, tmp_path, monkeypatch, recover_stale_running_job):
    # A file database provides distinct connections to the test and worker;
    # sharing one in-memory SQLite connection across threads would be misleading.
    engine = create_engine("sqlite:///" + str(tmp_path / "worker-test.sqlite"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(mail_app.sync, "SessionLocal", factory)
    server = MockIMAPServer({1: make_eml("<worker-job@tests.invalid>")})
    monkeypatch.setattr(mail_app.sync.imaplib, "IMAP4_SSL", server.factory)
    with factory() as session:
        queued = mail_app.sync.queue_sync(session, actor="test-operator")
        assert queued["status"] == "queued"
        if recover_stale_running_job:
            from server.db import utcnow
            run = session.get(mail_app.models.MailSyncRun, queued["id"])
            run.status = "running"
            run.started_at = utcnow() - timedelta(minutes=10)
            session.add(mail_app.models.MailSyncControl(account_key=mail_app.sync.account_key(), lease_owner="stopped-process", lease_until=utcnow() - timedelta(minutes=5)))
            session.commit()
    completed = False
    mail_app.sync.start_mail_worker()
    try:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            with factory() as session:
                run = session.get(mail_app.models.MailSyncRun, queued["id"])
                if run.status == "completed":
                    completed = True
                    break
                assert run.status != "failed", run.error_code
            threading.Event().wait(0.05)
    finally:
        mail_app.sync.stop_mail_worker()
    assert completed
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.InboxMessage)) == 1
    assert server.fetched_uids() == [1]
    engine.dispose()


def test_background_interval_and_enable_setting_persist_across_sessions(mail_app):
    with mail_app.factory() as session:
        mail_app.sync.configure_worker(session, enabled=True, poll_seconds=120, actor="test-operator")
    with mail_app.factory() as session:
        worker = mail_app.sync.get_mail_status(session)["worker"]
    assert worker["background_enabled"] is True and worker["poll_seconds"] == 120


def test_restart_during_blocked_fetch_resumes_checkpoint_with_one_connection(mail_app, tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'worker-restart.sqlite'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(mail_app.sync, 'SessionLocal', factory)
    server = MockIMAPServer({uid: make_eml(f'<restart-{uid}@tests.invalid>') for uid in (1, 2, 3)})
    entered, release = threading.Event(), threading.Event()
    active_lock = threading.Lock()
    active = peak = 0
    blocked_once = False

    def block_second_fetch(folder, uid):
        nonlocal blocked_once
        if folder == 'INBOX' and uid == 2 and not blocked_once:
            blocked_once = True
            entered.set()
            assert release.wait(8), 'Test did not release its blocked IMAP fetch'

    def connection_factory(*args, **kwargs):
        nonlocal active, peak
        connection = server.factory(*args, **kwargs)
        with active_lock:
            active += 1
            peak = max(peak, active)
        logout = connection.logout
        def tracked_logout():
            nonlocal active
            try:
                return logout()
            finally:
                with active_lock:
                    active -= 1
        connection.logout = tracked_logout
        return connection

    server.on_fetch = block_second_fetch
    monkeypatch.setattr(mail_app.sync.imaplib, 'IMAP4_SSL', connection_factory)
    with factory() as session:
        queued = mail_app.sync.queue_sync(session, actor='test-operator')
    mail_app.sync.start_mail_worker()
    try:
        assert entered.wait(5), 'Worker did not enter the blocking mock fetch'
        old_worker, old_signal = mail_app.sync._worker_thread, mail_app.sync._worker_stop
        with factory() as session:
            state = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
            assert state.last_uid == 1
        assert mail_app.sync.stop_mail_worker(timeout=0.01) is False
        assert old_signal.is_set()
        mail_app.sync.start_mail_worker()
        # A replacement is requested, but cannot overlap the old connection.
        assert mail_app.sync._worker_thread is old_worker
        assert mail_app.sync._worker_stop is old_signal and old_signal.is_set()
        assert server.connections == 1
        with factory() as session:
            assert mail_app.sync.get_mail_status(session)['worker']['status'] == 'restarting'
        release.set()
        deadline = time.monotonic() + 8
        completed = False
        while time.monotonic() < deadline:
            with factory() as session:
                run = session.get(mail_app.models.MailSyncRun, queued['id'])
                if run.status == 'completed':
                    completed = True
                    assert run.imported == 3 and run.errors == 0
                    break
                assert run.status != 'failed', run.error_code
            threading.Event().wait(0.03)
        assert completed, 'Requested replacement did not resume the same durable job'
        assert server.connections == 2 and peak == 1
        assert mail_app.sync._worker_thread is not old_worker
        assert server.fetched_uids() == [1, 2, 3]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(workflows.InboxMessage)) == 3
            assert session.scalar(select(func.count()).select_from(mail_app.models.MailSyncRun)) == 1
    finally:
        release.set()
        assert mail_app.sync.stop_mail_worker()
        engine.dispose()


def test_worker_interrupts_only_its_owned_socket_without_imap_mutations():
    from server.mail_sync import _interrupt_connection
    calls = []
    class OwnedSocket:
        def shutdown(self, how):
            calls.append(('shutdown', how))
        def close(self):
            calls.append(('socket_close',))
    class Connection:
        sock = OwnedSocket()
        def close(self):
            pytest.fail('IMAP CLOSE must not be used during shutdown')
        def store(self, *args):
            pytest.fail('IMAP STORE must not be used during shutdown')
    _interrupt_connection(Connection())
    assert [item[0] for item in calls] == ['shutdown', 'socket_close']


def test_missing_uidvalidity_stops_sync_before_fetching_or_advancing(mail_app):
    server = MockIMAPServer({1: make_eml("<unsafe-without-epoch@tests.invalid>")})
    original_factory = server.factory
    def without_epoch(*args, **kwargs):
        connection = original_factory(*args, **kwargs)
        original_response = connection.response
        connection.response = lambda name: ("UIDVALIDITY", [None]) if str(name).upper() == "UIDVALIDITY" else original_response(name)
        return connection
    with mail_app.factory() as session:
        result = mail_app.sync.sync_mailbox(session, actor=mail_app.actor, imap_factory=without_epoch)
    assert result["status"] == "failed" and result["error_code"] == "imap_uidvalidity_missing"
    assert inbox_rows(mail_app) == [] and server.fetched_uids() == []


def test_widening_history_range_resets_only_history_checkpoint(mail_app):
    server = MockIMAPServer({1: make_eml("<older-history@tests.invalid>"), 2: make_eml("<recent-history@tests.invalid>")})
    server.internal_dates[("INBOX", 1)] = (datetime.now(timezone.utc) - timedelta(days=200)).date()
    run_sync(mail_app, server, kind="history", days=90)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<recent-history@tests.invalid>"}
    run_sync(mail_app, server, kind="history", days=365)
    assert {row.message_id for row in inbox_rows(mail_app)} == {"<older-history@tests.invalid>", "<recent-history@tests.invalid>"}
    with mail_app.factory() as session:
        inbox_state = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == "INBOX"))
    assert inbox_state.history_days == 365 and inbox_state.last_uid == 0


def test_active_account_lease_prevents_a_second_sync_without_opening_socket(mail_app):
    from server.db import utcnow
    with mail_app.factory() as session:
        session.add(mail_app.models.MailSyncControl(account_key=mail_app.sync.account_key(), lease_owner="active-worker", lease_until=utcnow() + timedelta(minutes=2)))
        session.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("A second sync must not connect while another worker owns the lease")
    with mail_app.factory() as session:
        with pytest.raises(HTTPException) as caught:
            mail_app.sync.sync_mailbox(session, actor=mail_app.actor, imap_factory=forbidden)
    assert caught.value.status_code == 409 and caught.value.detail["code"] == "mail_sync_busy"
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(mail_app.models.MailSyncRun)) == 0
        assert session.get(mail_app.models.MailSyncControl, mail_app.sync.account_key()).lease_owner == "active-worker"


def test_queued_history_stop_is_durable_and_admin_only(mail_app):
    queued = mail_app.client.post("/api/mail/history", json={"days": 365, "batch_size": 10})
    assert queued.status_code == 202 and queued.json()["kind"] == "history"
    run_id = queued.json()["id"]
    mail_app.actor.role = "operator"
    assert mail_app.client.post(f"/api/mail/runs/{run_id}/stop").status_code == 403
    mail_app.actor.role = "admin"
    stopped = mail_app.client.post(f"/api/mail/runs/{run_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    with mail_app.factory() as session:
        run = session.get(mail_app.models.MailSyncRun, run_id)
        assert run.stop_requested is True and run.status == "stopped"
    assert inbox_rows(mail_app) == []
    assert mail_app.client.get("/api/mail/runs").json()[0]["stop_requested"] is True


def test_thread_combines_incoming_and_sent_in_chronological_order_without_other_cases(mail_app):
    root = make_eml("<thread-root@tests.invalid>", sent_at=datetime(2026, 10, 19, 9, tzinfo=timezone.utc))
    engineer = make_eml("<thread-engineer@tests.invalid>", references="<thread-root@tests.invalid>", sent_at=datetime(2026, 10, 19, 10, tzinfo=timezone.utc))
    followup = make_eml("<thread-followup@tests.invalid>", references="<thread-root@tests.invalid> <thread-engineer@tests.invalid>", sent_at=datetime(2026, 10, 19, 11, tzinfo=timezone.utc))
    server = MockIMAPServer({1: root, 2: followup, 3: make_eml("<unrelated-thread@tests.invalid>")}, {8: engineer})
    run_sync(mail_app, server)
    rows = {row.message_id: row for row in inbox_rows(mail_app)}
    response = mail_app.client.get(f"/api/inbox/{rows['<thread-followup@tests.invalid>'].id}/thread")
    assert response.status_code == 200
    thread = response.json()
    assert thread["case_id"] == rows["<thread-root@tests.invalid>"].case_id
    assert [message["message_id"] for message in thread["messages"]] == ["<thread-root@tests.invalid>", "<thread-engineer@tests.invalid>", "<thread-followup@tests.invalid>"]
    assert {message["direction"] for message in thread["messages"]} == {"incoming", "outgoing"}
    assert thread["tasks"] == []
    assert "raw_mime" not in response.text and "storage_path" not in response.text
    task = mail_app.client.post(f"/api/cases/{thread['case_id']}/tasks", json={"title": "Проверить применённую регулировку", "department": "КБ"}).json()
    updated = mail_app.client.get(f"/api/inbox/{rows['<thread-root@tests.invalid>'].id}/thread").json()
    assert [item["id"] for item in updated["tasks"]] == [task["id"]]


def test_manual_solution_candidate_is_private_idempotent_and_retains_mail_evidence(mail_app):
    import hashlib
    root = make_eml("<solution-root@tests.invalid>")
    reply = make_eml("<solution-engineer@tests.invalid>", references="<solution-root@tests.invalid>", subject="TEST: подтверждённые действия инженера")
    server = MockIMAPServer({1: root}, {2: reply})
    run_sync(mail_app, server)
    rows = {row.message_id: row for row in inbox_rows(mail_app)}
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.VerifiedSolution)) == 0
    payload = {"title": "PE38-12: проверенное устранение застревания", "equipment_model": "PE38-12", "symptoms": "Короб застревает при подаче", "actions": "Инженер подтвердил выполненную регулировку по руководству", "limitations": "Применимо только к PE38-12; проводить после отключения энергии"}
    first = mail_app.client.post(f"/api/inbox/{rows['<solution-root@tests.invalid>'].id}/solution-candidate", json=payload)
    assert first.status_code in (200, 201)
    candidate = first.json()
    assert candidate["existing"] is False
    assert candidate["status"] == "CANDIDATE" and candidate["visibility"] == "internal"
    assert candidate["source"]
    assert candidate["limitations"] == payload["limitations"]
    assert candidate["customer_confirmed"] is False
    # Creating from another message in the same case/model reuses the original
    # reviewed evidence rather than silently replacing the operator's actions.
    repeated = mail_app.client.post(f"/api/inbox/{rows['<solution-engineer@tests.invalid>'].id}/solution-candidate", json={**payload, "actions": "Другие действия требуют отдельного редактирования"}).json()
    assert repeated["existing"] is True and repeated["id"] == candidate["id"]
    assert repeated["actions"] == payload["actions"]
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.VerifiedSolution)) == 1
        assert workflows.search_verified_solutions(session, "застревание PE38-12", public_only=True) == []
        assert workflows.search_verified_solutions(session, "застревание PE38-12", public_only=False) == []
        origin = session.scalar(select(mail_app.models.MailSolutionOrigin))
        assert origin is not None
        persisted_evidence = str({column.name: getattr(origin, column.name) for column in origin.__table__.columns})
    assert "<solution-root@tests.invalid>" in persisted_evidence and "<solution-engineer@tests.invalid>" in persisted_evidence
    assert hashlib.sha256(root).hexdigest() in persisted_evidence and hashlib.sha256(reply).hexdigest() in persisted_evidence
    assert "raw_mime" not in candidate and "evidence_snapshot" not in candidate and "evidence" not in candidate


def test_candidate_requires_actions_and_cannot_request_publication_or_verification(mail_app):
    server = MockIMAPServer({1: make_eml("<candidate-validation@tests.invalid>")})
    run_sync(mail_app, server)
    message_id = inbox_rows(mail_app)[0].id
    path = f"/api/inbox/{message_id}/solution-candidate"
    payload = {"title": "PE38-12: подача", "equipment_model": "PE38-12", "symptoms": "Не подаётся короб", "actions": "Проверили регулировку"}
    assert mail_app.client.post(path, json={key: value for key, value in payload.items() if key != "actions"}).status_code == 422
    assert mail_app.client.post(path, json={**payload, "actions": "  "}).status_code == 422
    assert mail_app.client.post(path, json={**payload, "status": "VERIFIED"}).status_code == 422
    assert mail_app.client.post(path, json={**payload, "visibility": "public"}).status_code == 422
    with mail_app.factory() as session:
        assert session.scalar(select(func.count()).select_from(workflows.VerifiedSolution)) == 0
    del mail_app.app.dependency_overrides[require_staff]
    assert mail_app.client.post(path, json=payload).status_code == 401
    assert mail_app.client.get(f"/api/inbox/{message_id}/thread").status_code == 401


@pytest.mark.parametrize("filename,payload", [("camera.jpg", b"%PDF-1.7\nnot a JPEG"), ("recording.wav", b"MZ-fake audio"), ("clip.mp4", b"MZ-fake video"), ("notes.txt", b"MZ-fake executable"), ("notes.md", b"binary\x00payload"), ("archive.csv", b"PK\x03\x04zip archive")])
def test_attachment_extensions_do_not_override_binary_signature(mail_app, filename, payload):
    mime, reason = mail_app.sync._attachment_allowed(filename, payload)
    assert mime is None and reason


def test_image_extension_must_match_decoded_format_but_real_utf8_text_is_allowed(mail_app):
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
    png = stream.getvalue()
    assert mail_app.sync._attachment_allowed("drawing.png", png) == ("image/png", None)
    assert mail_app.sync._attachment_allowed("drawing.jpg", png)[0] is None
    assert mail_app.sync._attachment_allowed("заметки.txt", "Проверено инженером: текст UTF-8".encode()) == ("text/plain", None)
