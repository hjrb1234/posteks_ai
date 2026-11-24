"""Realistic imaplib FETCH shapes, ranged reads and durable failed UID recovery.

These are protocol fixtures, not a claim of live access to a Yandex mailbox.
"""
from collections import deque
import re
import threading

import pytest
from sqlalchemy import select

from server import mail_sync
from test_mail_sync import mail_app, make_eml, MockIMAPServer, MockIMAPConnection, run_sync, inbox_rows


UID = 73


class FetchScript:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def uid(self, command, uid, query):
        assert command.lower() == 'fetch' and int(uid) == UID
        assert 'BODY[' not in query.replace('BODY.PEEK[', '')  # no Seen-setting request
        self.calls.append(query)
        return self.responses.popleft()


def size_response(size, uid=UID):
    return 'OK', [f'12 (RFC822.SIZE {size} UID {uid})'.encode()]


def body_response(raw, size=None, *, uid=UID, origin=None, trailing_uid=False):
    size = len(raw) if size is None else size
    attributes = '' if trailing_uid else f'UID {uid} '
    offset = '' if origin is None else f'<{origin}>'
    prefix = f'12 ({attributes}BODY[]{offset} {{{len(raw)}}}'.encode()
    suffix = f' RFC822.SIZE {size}' + (f' UID {uid}' if trailing_uid else '') + ')'
    return 'OK', [(prefix, raw), suffix.encode()]


@pytest.mark.parametrize('trailing_uid', [False, True])
def test_size_and_literal_metadata_order_accepts_real_imaplib_shapes(trailing_uid):
    raw = make_eml('<compatible@tests.invalid>')
    script = FetchScript([size_response(len(raw)), body_response(raw, trailing_uid=trailing_uid)])
    assert mail_sync._fetch(script, UID) == (raw, None)
    assert script.calls == ['(UID RFC822.SIZE)', '(UID RFC822.SIZE BODY.PEEK[])']


def test_unsolicited_fetch_other_uid_is_not_joined_into_target_message():
    raw = make_eml('<target@tests.invalid>')
    unrelated = b'From: secret-other@tests.invalid\r\n\r\nPRIVATE_UNRELATED_MESSAGE'
    status, data = body_response(raw)
    _, other = body_response(unrelated, uid=999)
    script = FetchScript([('OK', [b'13 (UID 999 RFC822.SIZE 4)', size_response(len(raw))[1][0]]), (status, other + data)])
    fetched, reason = mail_sync._fetch(script, UID)
    assert fetched == raw and reason is None and b'PRIVATE_UNRELATED' not in fetched


def test_body_literal_cannot_forge_target_uid_or_size_metadata():
    raw = b'From: client@tests.invalid\r\n\r\nUID 73 RFC822.SIZE 99999999'
    script = FetchScript([size_response(len(raw)), body_response(raw)])
    assert mail_sync._fetch(script, UID) == (raw, None)


def test_rejected_full_read_retries_same_uid_with_bounded_readonly_ranges(monkeypatch):
    raw = b'0123456789abcdefghij'
    monkeypatch.setattr(mail_sync, 'FETCH_CHUNK_BYTES', 8)
    script = FetchScript([size_response(len(raw)), ('NO', [b'[UNAVAILABLE] full fetch failed']),
        body_response(raw[:8], len(raw), origin=0), body_response(raw[8:16], len(raw), origin=8),
        body_response(raw[16:], len(raw), origin=16)])
    beats = []
    assert mail_sync._fetch(script, UID, heartbeat=lambda: beats.append(True)) == (raw, None)
    assert len(beats) == 3
    assert script.calls[2:] == ['(UID RFC822.SIZE BODY.PEEK[]<0.8>)',
        '(UID RFC822.SIZE BODY.PEEK[]<8.8>)', '(UID RFC822.SIZE BODY.PEEK[]<16.4>)']


def test_missing_full_literal_can_recover_by_partial_read():
    raw = make_eml('<recover-empty-full@tests.invalid>')
    script = FetchScript([size_response(len(raw)), ('OK', [None, b'12 (UID 73)']), body_response(raw, origin=0)])
    assert mail_sync._fetch(script, UID) == (raw, None)


@pytest.mark.parametrize('response', [
    body_response(b'12345678', 8, uid=999, origin=0),
    body_response(b'12345678', 8, origin=1),
    body_response(b'1234', 8, origin=0),
    ('OK', [(b'12 (UID 73 RFC822.SIZE 8 BODY[]<0> {8}', b'1234'), b')']),
    ('OK', [b'From: unframed@tests.invalid\r\n\r\nnot a FETCH literal']),
])
def test_wrong_uid_origin_short_or_unframed_body_never_becomes_imported_data(response):
    script = FetchScript([size_response(8), ('NO', [b'full unavailable']), response])
    with pytest.raises(mail_sync.MailFailure) as caught:
        mail_sync._fetch(script, UID)
    assert caught.value.code == 'imap_body_incomplete'
    assert caught.value.detail['uid'] == UID


def test_failed_read_contains_safe_stage_uid_status_but_not_server_raw_text():
    secret = b'private-user@example.com sensitive-token-value'
    script = FetchScript([size_response(8), ('NO', [secret]), ('NO', [b'[UNAVAILABLE] '+secret])])
    with pytest.raises(mail_sync.MailFailure) as caught:
        mail_sync._fetch(script, UID)
    error = caught.value
    assert error.code == 'imap_fetch_failed'
    assert error.detail == {'uid': UID, 'stage': 'body_partial', 'imap_status': 'NO', 'response_code': 'UNAVAILABLE'}
    assert 'UID 73' in error.message and 'NO [UNAVAILABLE]' in error.message
    assert 'example.com' not in str(error) and 'sensitive-token' not in str(error)


@pytest.mark.parametrize('result,data', [
    ('NO', [b'[AUTHENTICATIONFAILED] private information']),
    ('NO', [b'[NOPERM] private information']),
    ('BAD', [b'invalid command']), ('BYE', [b'connection is closing']),
])
def test_authentication_and_protocol_failure_do_not_probe_more_body_commands(result, data):
    script = FetchScript([size_response(8), (result, data)])
    with pytest.raises(mail_sync.MailFailure) as caught:
        mail_sync._fetch(script, UID)
    assert caught.value.detail['stage'] == 'body' and len(script.calls) == 2


def test_missing_size_and_oversized_message_never_download_body():
    script = FetchScript([size_response(mail_sync.MAX_IMAP_BYTES + 1)])
    assert mail_sync._fetch(script, UID) == (None, 'message_too_large')
    assert len(script.calls) == 1
    for data in [[None, ()], [b'12 (UID 999 RFC822.SIZE 8)']]:
        script = FetchScript([('OK', data)])
        with pytest.raises(mail_sync.MailFailure, match='UID 73') as caught:
            mail_sync._fetch(script, UID)
        assert caught.value.code == 'imap_size_missing' and len(script.calls) == 1


class PartialOnlyConnection(MockIMAPConnection):
    def uid(self, command, *args):
        if command.lower() != 'fetch' or 'BODY.PEEK[]' not in str(args[1]).upper():
            return super().uid(command, *args)
        uid, query = int(args[0]), str(args[1])
        self.server.calls.append(('fetch', self.folder, uid, query))
        match = re.search(r'BODY\.PEEK\[\]<(\d+)\.(\d+)>', query, re.I)
        if match is None or getattr(self.server, 'block_partial', False) or uid in getattr(self.server, 'blocked_uids', set()):
            return 'NO', [b'[UNAVAILABLE] email fetch failed for hidden@example.com']
        offset, count = map(int, match.groups())
        raw = self.server.folders[self.folder][uid]
        piece = raw[offset:offset+count]
        if callback := getattr(self.server, 'on_partial', None):
            callback(uid, offset)
        return 'OK', [(f'12 (UID {uid} RFC822.SIZE {len(raw)} BODY[]<{offset}> {{{len(piece)}}}'.encode(), piece), b')']


class PartialOnlyServer(MockIMAPServer):
    def factory(self, *args, **kwargs):
        self.connections += 1
        return PartialOnlyConnection(self)


def test_year_history_recovers_full_fetch_rejection_and_drains_every_batch(mail_app):
    server = PartialOnlyServer({uid: make_eml(f'<ranged-{uid}@tests.invalid>') for uid in [1, 2, 3]})
    first = run_sync(mail_app, server, kind='history', batch_size=1)
    assert first['status'] == 'completed' and first['has_more']
    run_sync(mail_app, server, kind='history', batch_size=1, run_id=first['id'])
    last = run_sync(mail_app, server, kind='history', batch_size=1, run_id=first['id'])
    assert last['status'] == 'completed' and not last['has_more'] and last['imported'] == 3
    assert len(inbox_rows(mail_app)) == 3
    with mail_app.factory() as session:
        state = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
        assert state.history_last_uid == 3 and state.history_completed
    assert not any(call[0] in {'store','copy','move','expunge'} for call in server.calls)


def test_failed_range_retains_uid_and_exposes_safe_diagnostic_then_resumes(mail_app):
    server = PartialOnlyServer({1: make_eml('<retry-range@tests.invalid>')})
    server.block_partial = True
    first = run_sync(mail_app, server, kind='history')
    assert first['status'] == 'completed' and first['error_code'] == 'partial_import'
    assert first['fetched'] == 0 and first['imported'] == 0
    failure = next(part for part in first['progress'] if part['folder'] == 'INBOX')
    assert failure['issues'][0]['detail'] == {'uid': 1, 'stage': 'body_partial', 'imap_status': 'NO', 'response_code': 'UNAVAILABLE'}
    assert failure['history_last_uid'] == 1 and 'hidden@example.com' not in str(first)
    assert inbox_rows(mail_app) == []
    with mail_app.factory() as session:
        remote = session.scalar(select(mail_app.models.MailRemoteMessage))
        assert remote.uid == 1 and remote.status == 'retryable' and remote.inbox_id is None
        state = mail_app.sync.get_mail_status(session)['history']
        assert state['status'] == 'partial' and not state['completed']
        assert state['unresolved'] == [{'folder': 'INBOX', 'uid': 1, 'error_code': 'imap_fetch_failed'}]
    server.block_partial = False
    retried = run_sync(mail_app, server, kind='history')
    assert retried['status'] == 'completed' and retried['errors'] == 0 and retried['imported'] == 1
    assert len(inbox_rows(mail_app)) == 1


def test_one_broken_uid_does_not_block_later_healthy_year_mail_and_retries_after_cursor(mail_app):
    server = PartialOnlyServer({uid: make_eml(f'<gap-{uid}@tests.invalid>') for uid in [1, 2, 3]})
    server.blocked_uids = {1}
    first = run_sync(mail_app, server, kind='history', batch_size=1)
    second = run_sync(mail_app, server, kind='history', batch_size=1, run_id=first['id'])
    last = run_sync(mail_app, server, kind='history', batch_size=1, run_id=first['id'])
    assert first['has_more'] and second['has_more'] and not last['has_more']
    assert last['imported'] == 2 and last['errors'] == 1 and last['error_code'] == 'partial_import'
    with mail_app.factory() as session:
        folder = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
        assert folder.history_last_uid == 3
        history = mail_app.sync.get_mail_status(session)['history']
        assert history['status'] == 'partial' and history['unresolved_skips'] == 1 and not history['completed']
    # This same bounded job did not loop back to the failed first message.
    assert len([c for c in server.calls if c[:3] == ('fetch', 'INBOX', 1) and 'BODY.PEEK[]<' in c[3]]) == 1
    server.blocked_uids.clear()
    retry = run_sync(mail_app, server, kind='history')
    assert retry['imported'] == 1 and retry['errors'] == 0 and retry['fetched'] == 1
    assert len(inbox_rows(mail_app)) == 3
    with mail_app.factory() as session:
        history = mail_app.sync.get_mail_status(session)['history']
        assert history['completed'] and history['unresolved_skips'] == 0
        folder = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
        assert folder.history_last_uid == 3  # retry did not rewind it to1


def test_repeated_read_rejections_stop_after_three_even_with_one_message_batches(mail_app):
    server = PartialOnlyServer({uid: make_eml(f'<systemic-{uid}@tests.invalid>') for uid in range(1, 9)})
    server.block_partial = True
    run = run_sync(mail_app, server, kind='history', batch_size=1)
    run = run_sync(mail_app, server, kind='history', batch_size=1, run_id=run['id'])
    run = run_sync(mail_app, server, kind='history', batch_size=1, run_id=run['id'])
    assert run['status'] == 'failed' and run['error_code'] == 'imap_repeated_fetch_failed'
    assert run['fetched'] == 0 and run['imported'] == 0
    assert {c[2] for c in server.calls if c[0] == 'fetch'} == {1, 2, 3}
    with mail_app.factory() as session:
        gaps = list(session.scalars(select(mail_app.models.MailRemoteMessage)))
        assert {r.uid for r in gaps} == {1, 2, 3} and all(r.status == 'retryable' for r in gaps)
        folder = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
        assert folder.history_last_uid == 3 and not folder.history_completed
        history = mail_app.sync.get_mail_status(session)['history']
        assert history['status'] == 'failed' and not history['completed']
        assert 'История просмотрена' not in history['notice']


def test_shutdown_interrupts_between_ranged_reads_without_acknowledging_uid(mail_app, monkeypatch):
    server = PartialOnlyServer({1: make_eml('<paused-range@tests.invalid>')})
    monkeypatch.setattr(mail_sync, 'FETCH_CHUNK_BYTES', 16)
    signal = threading.Event()
    server.on_partial = lambda uid, offset: signal.set()
    paused = run_sync(mail_app, server, kind='history', stop_event=signal)
    assert paused['status'] == 'queued' and paused['has_more'] and paused['errors'] == 0
    with mail_app.factory() as session:
        folder = session.scalar(select(mail_app.models.MailFolderState).where(mail_app.models.MailFolderState.folder == 'INBOX'))
        assert folder.history_last_uid == 0
        assert session.query(mail_app.models.MailRemoteMessage).count() == 0
    signal.clear()
    server.on_partial = None
    resumed = run_sync(mail_app, server, kind='history', run_id=paused['id'])
    assert resumed['status'] == 'completed' and resumed['imported'] == 1 and resumed['errors'] == 0
