"""Mail selfcheck is a bounded protocol probe, not an importer or year promise."""
from email.message import EmailMessage
import hashlib
import imaplib
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scripts.check_mail_history import check_mail_history
from server import mail_sync, workflows
from server.config import settings
from server.db import Base
from server.mail_models import MailFolderState, MailRemoteMessage, MailSyncControl, MailSyncRun

PRIVATE_PASSWORD = 'TEST_PASSWORD_NEVER_IN_REPORT'
PRIVATE_BODY = 'TEST_PRIVATE_EMAIL_BODY_NEVER_IN_REPORT'
PRIVATE_ADDRESS = 'private-recipient@tests.invalid'


@pytest.fixture
def mailbox(monkeypatch):
    for key in ('IMAP_HOST', 'IMAP_USER', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_PORT',
                'MAIL_IMAP_HOST', 'MAIL_USERNAME', 'MAIL_ADDRESS', 'MAIL_APP_PASSWORD',
                'MAIL_MAILBOX', 'MAIL_INBOX_FOLDER', 'MAIL_SENT_FOLDER'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MAIL_IMAP_HOST', 'imap.yandex.com')
    monkeypatch.setenv('MAIL_USERNAME', PRIVATE_ADDRESS)
    monkeypatch.setenv('MAIL_APP_PASSWORD', PRIVATE_PASSWORD)
    monkeypatch.setattr(settings, 'app_mode', 'SAFE')
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def raw_message(uid):
    message = EmailMessage()
    message['From'] = PRIVATE_ADDRESS
    message['To'] = 'service@tests.invalid'
    message['Subject'] = 'TEST: mail selfcheck'
    message['Message-ID'] = f'<probe-{uid}@tests.invalid>'
    message.set_content(PRIVATE_BODY)
    return message.as_bytes()


class Mailbox:
    def __init__(self, *, fail_login=False, fail_uid=None, empty=False):
        self.calls = []
        self.folder = None
        self.logged_out = False
        self.fail_login, self.fail_uid = fail_login, fail_uid
        self.messages = {'INBOX': {} if empty else {uid: raw_message(uid) for uid in (1, 2, 3)},
                         'Sent': {} if empty else {uid: raw_message(uid) for uid in (10, 11)}}

    def factory(self, host, port, **kwargs):
        self.calls.append(('connect', host, port, kwargs['timeout']))
        assert kwargs['ssl_context'].check_hostname is True
        return self

    def login(self, username, password):
        assert username == PRIVATE_ADDRESS and password == PRIVATE_PASSWORD
        self.calls.append(('login',))
        if self.fail_login:
            raise imaplib.IMAP4.error(PRIVATE_PASSWORD + PRIVATE_ADDRESS + PRIVATE_BODY)
        return 'OK', [b'logged in']

    def list(self):
        self.calls.append(('list',))
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, folder, readonly=False):
        assert readonly is True
        self.folder = folder.strip('"')
        self.calls.append(('select', self.folder, readonly))
        return 'OK', [str(len(self.messages[self.folder])).encode()]

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return 'UIDVALIDITY', [b'101' if self.folder == 'INBOX' else b'202']

    def uid(self, command, *args):
        self.calls.append(('uid', self.folder, command, *args))
        if command == 'search':
            assert args[0] is None and args[1].startswith('SINCE ')
            return 'OK', [b' '.join(str(uid).encode() for uid in self.messages[self.folder])]
        assert command == 'fetch'
        uid, query = int(args[0]), args[1]
        raw = self.messages[self.folder][uid]
        if query == '(UID RFC822.SIZE)':
            return 'OK', [f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode()]
        assert 'BODY.PEEK[]' in query and 'BODY[]' not in query
        if uid == self.fail_uid:
            return 'NO', [('[UNAVAILABLE] ' + PRIVATE_PASSWORD + PRIVATE_ADDRESS + PRIVATE_BODY).encode()]
        return 'OK', [(f'1 (UID {uid} RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}'.encode(), raw), b')']

    def logout(self):
        self.logged_out = True
        self.calls.append(('logout',))
        return 'BYE', []


def by_id(records, identifier):
    return next(item for item in records if item['id'] == identifier)


def unchanged_business_tables(session):
    for model in (workflows.ServiceCase, workflows.InternalTask, workflows.Approval,
                  workflows.VerifiedSolution, workflows.AuditEvent, MailSyncControl):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_local_check_makes_no_connection_or_database_writes(mailbox):
    server = Mailbox()
    report = check_mail_history(mailbox, imap_factory=server.factory)
    assert by_id(report, 'mail.config')['status'] == 'pass'
    assert by_id(report, 'mail.history')['status'] == 'warn'
    assert by_id(report, 'mail.live')['status'] == 'skipped'
    assert server.calls == []
    unchanged_business_tables(mailbox)
    assert mailbox.scalar(select(func.count()).select_from(MailSyncRun)) == 0


def test_missing_credentials_do_not_connect_or_echo_values(mailbox, monkeypatch):
    monkeypatch.delenv('MAIL_APP_PASSWORD')
    server = Mailbox()
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    assert by_id(report, 'mail.config')['evidence']['missing_variable_names'] == ['MAIL_APP_PASSWORD']
    assert by_id(report, 'mail.live')['status'] == 'skipped' and server.calls == []
    assert PRIVATE_ADDRESS not in json.dumps(report)


def test_effective_test_mode_prohibits_network(mailbox, monkeypatch):
    monkeypatch.setattr(settings, 'app_mode', 'TEST')
    server = Mailbox()
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    assert by_id(report, 'mail.live')['status'] == 'skipped' and server.calls == []


def test_live_check_reads_two_samples_total_and_reports_unimported_year(mailbox):
    server = Mailbox()
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    live = by_id(report, 'mail.live')
    assert live['status'] == 'pass' and live['evidence']['read_only'] is True
    assert [sample['uid'] for sample in live['evidence']['samples']] == [1, 10]
    coverage = by_id(report, 'mail.coverage_live')
    assert coverage['status'] == 'warn'
    assert coverage['evidence']['year_messages_found'] == coverage['evidence']['missing_uids_count'] == 5
    assert coverage['evidence']['all_visible_year_uids_stored'] is False
    assert server.logged_out
    encoded = json.dumps(report)
    assert all(secret not in encoded for secret in (PRIVATE_PASSWORD, PRIVATE_ADDRESS, PRIVATE_BODY))
    assert all(call[2] in {'search', 'fetch'} for call in server.calls if call[0] == 'uid')
    unchanged_business_tables(mailbox)
    assert mailbox.scalar(select(func.count()).select_from(workflows.InboxMessage)) == 0
    assert mailbox.scalar(select(func.count()).select_from(MailFolderState)) == 0


def test_sample_limit_one_is_not_per_folder(mailbox):
    server = Mailbox()
    report = check_mail_history(mailbox, live=True, sample_limit=1, imap_factory=server.factory)
    assert len(by_id(report, 'mail.live')['evidence']['samples']) == 1
    body_fetches = [call for call in server.calls if call[0] == 'uid' and call[2] == 'fetch' and 'BODY.PEEK[]' in call[4]]
    assert len(body_fetches) == 1


def test_successful_connection_and_empty_search_do_not_claim_imported_year(mailbox):
    server = Mailbox(empty=True)
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    assert by_id(report, 'mail.coverage_live')['status'] == 'warn'
    assert by_id(report, 'mail.coverage_live')['evidence']['year_messages_found'] == 0
    assert by_id(report, 'mail.live')['evidence']['samples'] == []


def test_authentication_failure_is_useful_but_does_not_leak_server_text(mailbox):
    server = Mailbox(fail_login=True)
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    live = by_id(report, 'mail.live')
    assert live['status'] == 'fail' and live['evidence']['code'] == 'imap_authentication_failed'
    assert server.logged_out
    encoded = json.dumps(report)
    assert all(secret not in encoded for secret in (PRIVATE_PASSWORD, PRIVATE_ADDRESS, PRIVATE_BODY))


def test_fetch_failure_preserves_safe_uid_stage_and_does_not_mutate_checkpoints(mailbox):
    server = Mailbox(fail_uid=1)
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    live = by_id(report, 'mail.live')
    assert live['status'] == 'fail'
    assert live['evidence']['uid'] == 1 and live['evidence']['stage'] == 'body_partial'
    assert live['evidence']['imap_status'] == 'NO' and live['evidence']['response_code'] == 'UNAVAILABLE'
    assert server.logged_out
    assert mailbox.scalar(select(func.count()).select_from(MailFolderState)) == 0
    assert mailbox.scalar(select(func.count()).select_from(MailRemoteMessage)) == 0
    assert PRIVATE_BODY not in json.dumps(report)


def test_current_uid_coverage_is_compared_to_stored_records_not_just_last_run(mailbox):
    server = Mailbox()
    key = mail_sync.account_key()
    for folder, messages in server.messages.items():
        for uid, raw in messages.items():
            inbox = workflows.InboxMessage(message_id=f'<{folder}-{uid}@tests.invalid>',
                content_hash=hashlib.sha256(raw + folder.encode()).hexdigest(), subject='TEST', sender=PRIVATE_ADDRESS,
                sender_name='', recipients=[], body=PRIVATE_BODY, references=[], attachments=[], raw_mime=raw, source='IMAP_HISTORY')
            mailbox.add(inbox);mailbox.flush()
            mailbox.add(MailRemoteMessage(account_key=key, folder=folder, uidvalidity='101' if folder == 'INBOX' else '202',
                uid=uid, inbox_id=inbox.id, status='imported', direction='incoming' if folder == 'INBOX' else 'outgoing'))
    mailbox.commit()
    report = check_mail_history(mailbox, live=True, imap_factory=server.factory)
    coverage = by_id(report, 'mail.coverage_live')
    assert coverage['status'] == 'pass' and coverage['evidence']['all_visible_year_uids_stored'] is True
    assert coverage['evidence']['attachments_content_verified'] is False
    assert coverage['evidence']['model_learning_verified'] is False
    assert by_id(report, 'mail.history')['status'] == 'warn', 'No historical run was created by a diagnostic probe'
    unchanged_business_tables(mailbox)


def test_local_report_requires_both_completed_folders_and_does_not_write_control(mailbox):
    key = mail_sync.account_key()
    for kind, folder in [('inbox', 'INBOX'), ('sent', 'Sent')]:
        mailbox.add(MailFolderState(account_key=key, folder=folder, kind=kind,
            history_days=365, history_last_uid=3, history_completed=True, uidvalidity='101'))
    mailbox.add(MailSyncRun(account_key=key, kind='history', days=365, status='completed', errors=0, has_more=False))
    mailbox.commit()
    report = check_mail_history(mailbox)
    assert by_id(report, 'mail.history')['status'] == 'pass'
    assert by_id(report, 'mail.history')['evidence']['full_live_coverage_verified'] is False
    unchanged_business_tables(mailbox)


def test_pending_caller_writes_are_not_autoflushed(mailbox):
    draft = workflows.ServiceCase(title='Not part of mail selfcheck')
    mailbox.add(draft)
    check_mail_history(mailbox)
    assert draft.id is None and draft in mailbox.new
