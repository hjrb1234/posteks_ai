"""Private archive ingestion uses synthetic exports and real file extraction.

These checks do not establish that a real MAX group was exported completely.
"""
from io import BytesIO
import hashlib
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select

from test_history_import import history_app
from server import history_import, knowledge, workflows
from server.auth import require_staff
from server.models import AppSetting, ChatMessage, Document, Escalation


def staff(env):
    client, app, factory, path = env
    app.dependency_overrides[require_staff] = lambda: SimpleNamespace(username='archive-reviewer', role='admin')
    return client, factory, path


def bundle(messages, files=None, **extra):
    buffer = BytesIO()
    manifest = {'schema': 'posteks.archive.v1', 'messages': messages, **extra}
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def message(identifier='m1', text='TEST: неисправность датчика, возможно проверить разъём.', **extra):
    return {'id': identifier, 'conversation_id': 'synthetic-chat', 'date': '2026-11-01T13:00:00+05:00',
            'sender': 'Тестовый сотрудник', 'sender_id': 'synthetic-user', 'text': text, **extra}


def upload(client, raw, name='history.zip'):
    response = client.post('/api/history/import', files={'file': (name, raw, 'application/zip')},
                          data={'confirmed': 'true', 'source': 'MAX'})
    assert response.status_code == 201, response.text
    return response.json()


def test_archive_requests_and_historical_success_never_start_business_actions(history_app):
    client, factory, _ = staff(history_app)
    raw = bundle([message(text='Пришлите счёт на датчик. Возможно неисправен привод. Заказ №13 отправлен. После проверки всё заработало.')])
    record = upload(client, raw)
    detail = client.get('/api/history/imports/' + record['id']).json()
    assert detail['coverage']['completeness'] == 'unverified'
    assert detail['workflow_effects'] == {'active_cases_created': 0, 'drafts_created': 0, 'outgoing_actions': 0}
    stored = client.get('/api/history/imports/' + record['id'] + '/messages').json()['messages'][0]
    assert {'commercial_request', 'technical_discussion', 'hypothesis', 'order_history', 'historical_solution_claim'} <= set(stored['categories'])
    assert stored['classification_status'] == 'heuristic_unreviewed'
    with factory() as session:
        for model in (workflows.ServiceCase, workflows.Approval, workflows.InternalTask,
                      workflows.VerifiedSolution, workflows.InboxMessage, Escalation, ChatMessage):
            assert session.scalar(select(func.count()).select_from(model)) == 0, model
        document = session.get(Document, record['document_id'])
        assert document.visibility == 'internal'
        assert document.indexing_details['source_kind'] == 'unreviewed_history'


def test_zip_preserves_message_ids_reply_links_original_bytes_and_file_states(history_app):
    client, factory, path = staff(history_app)
    import pymupdf
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 60), 'TEST technical attachment: conveyor sensor observation.')
    original_pdf = pdf.tobytes()
    pdf.close()
    video = b'\x00\x00\x00\x18ftypmp42SYNTHETIC_VIDEO_NOT_REAL_RECORDING'
    rows = [message(), message('m2', '', reply_to_id='m1', attachments=[
        {'path': 'files/observation.pdf', 'sha256': hashlib.sha256(original_pdf).hexdigest()},
        {'path': 'files/video.mp4'}, {'path': 'files/not-downloaded.png'}])]
    raw = bundle(rows, {'files/observation.pdf': original_pdf, 'files/video.mp4': video},
                 coverage={'requested_period': 'test-fragment'})
    preview = client.post('/api/history/preview', files={'file': ('fragment.zip', raw)}).json()
    assert preview['message_count'] == 2 and preview['attachments_present'] == 2
    assert len(preview['gaps']) == 1
    record = upload(client, raw)
    detail = client.get('/api/history/imports/' + record['id']).json()
    assert detail['stages']['messages_received'] == 2
    assert detail['stages']['attachments_present'] == 2
    assert detail['stages']['attachment_contents_processed'] == 1
    assert detail['stages']['searchable_documents'] == 2  # transcript + real PDF
    assert detail['stages']['available_for_client_answers'] is False
    attachments = {a['filename']: a for a in detail['attachments']}
    assert attachments['video.mp4']['content_state'] == 'unsupported'
    assert attachments['not-downloaded.png']['file_state'] == 'missing'
    response = client.get(attachments['observation.pdf']['download_url'])
    assert response.content == original_pdf
    assert client.get(record['raw_url']).content == raw
    records = client.get('/api/history/imports/' + record['id'] + '/messages').json()['messages']
    assert records[1]['id'] == 'm2' and records[1]['reply_to_id'] == 'm1'
    assert records[1]['sender_id'] == 'synthetic-user'
    assert records[1]['text'] == ''
    # Attachment sub-ledgers are not misreported as separate chat imports.
    assert len(client.get('/api/history/imports').json()) == 1
    client.app.dependency_overrides.pop(require_staff)
    assert client.get(attachments['observation.pdf']['download_url']).status_code == 401
    assert client.get('/api/history/imports/' + record['id'] + '/messages').status_code == 401


def test_overlapping_parts_dedupe_messages_and_retain_changed_message_versions(history_app):
    client, factory, _ = staff(history_app)
    first = upload(client, bundle([message('m1'), message('m2', 'TEST: исходный ответ')]))
    same = upload(client, bundle([message('m1'), message('m2', 'TEST: исходный ответ')], coverage={'batch': 2}))
    assert same['new_message_count'] == 0 and same['duplicate_message_count'] == 2
    assert same['document_id'] == first['document_id']
    changed = upload(client, bundle([message('m2', 'TEST: исправленный ответ'), message('m3', 'TEST: новая запись')]))
    assert changed['new_message_count'] == 2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 2
        ledgers = session.scalars(select(AppSetting).where(AppSetting.key.startswith(history_import.MESSAGE_PREFIX))).all()
        assert len(ledgers) == 3
        edited = next(row for row in ledgers if row.value['message_id'] == 'm2')
        assert len(edited.value['versions']) == 2
    old = client.get('/api/history/imports/' + first['id'] + '/messages').json()['messages'][1]
    new = client.get('/api/history/imports/' + changed['id'] + '/messages').json()['messages'][0]
    assert old['text'] == 'TEST: исходный ответ' and new['text'] == 'TEST: исправленный ответ'


def test_duplicate_upload_resumes_failed_indexing_without_a_new_document(history_app, monkeypatch):
    client, factory, _ = staff(history_app)
    def failed(identifier):
        with factory() as session:
            doc = session.get(Document, identifier)
            doc.status = 'error'
            doc.error = 'TEST: interrupted indexing'
            session.commit()
    monkeypatch.setattr(history_import, 'ingest_document', failed)
    raw = bundle([message()])
    first = upload(client, raw)
    assert client.get('/api/history/imports/' + first['id']).json()['stages']['searchable_documents'] == 0
    monkeypatch.setattr(history_import, 'ingest_document', knowledge.ingest_document)
    retried = upload(client, raw)
    assert retried['duplicate'] is True and retried['document_id'] == first['document_id']
    assert client.get('/api/history/imports/' + first['id']).json()['stages']['searchable_documents'] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_attachment_processing_continues_in_bounded_batches_and_dedupes_files(history_app):
    client, factory, _ = staff(history_app)
    files = {f'files/{n}.txt': f'TEST: unique technical attachment {n}'.encode() for n in range(11)}
    raw = bundle([message(attachments=[{'path': name} for name in files])], files)
    first = upload(client, raw)
    detail = client.get('/api/history/imports/' + first['id']).json()
    assert detail['stages']['attachment_contents_processed'] == 10
    again = upload(client, raw)
    assert again['duplicate']
    detail = client.get('/api/history/imports/' + first['id']).json()
    assert detail['stages']['attachment_contents_processed'] == 11
    upload(client, raw)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 12


def test_unknown_fields_remain_unknown_and_duplicate_rows_in_one_part_are_safe(history_app):
    client, factory, _ = staff(history_app)
    raw = bundle([{'text': 'TEST: no metadata'}, {'text': 'TEST: no metadata'}])
    record = upload(client, raw)
    assert record['duplicate_message_count'] == 1 and record['new_message_count'] == 1
    row = client.get('/api/history/imports/' + record['id'] + '/messages').json()['messages'][0]
    assert row['sender'] == '' and row['date'] == '' and row['id'] == ''
    assert {'sender', 'date', 'id', 'reply_to_id'} <= set(row['unknown_fields'])


def test_two_exported_versions_of_the_same_id_in_one_part_preserve_both(history_app):
    client, factory, _ = staff(history_app)
    record = upload(client, bundle([message(text='TEST: original'), message(text='TEST: corrected')]))
    assert record['new_message_count'] == 2
    messages = client.get('/api/history/imports/' + record['id'] + '/messages').json()['messages']
    assert [row['text'] for row in messages] == ['TEST: original', 'TEST: corrected']
    with factory() as session:
        ledgers = session.scalars(select(AppSetting).where(AppSetting.key.startswith(history_import.MESSAGE_PREFIX))).all()
        assert len(ledgers) == 1 and len(ledgers[0].value['versions']) == 2


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'C:/secret.txt', 'http://example.invalid/file.txt'])
def test_zip_rejects_untrusted_member_and_attachment_paths(name):
    with pytest.raises(ValueError):
        history_import.parse_history(bundle([message(attachments=[{'path': name}])], {name: b'TEST'}), 'bad.zip')


def test_failed_commit_is_retryable_and_does_not_leave_archived_actions(history_app, monkeypatch):
    _, factory, path = staff(history_app)
    raw = bundle([message(attachments=[{'path': 'files/a.txt'}])], {'files/a.txt': b'TEST: attachment'})
    parsed = history_import.parse_history(raw, 'history.zip')
    with factory() as session:
        monkeypatch.setattr(session, 'commit', lambda: (_ for _ in ()).throw(RuntimeError('TEST interruption')))
        with pytest.raises(RuntimeError):
            history_import.save_history(session, raw, 'history.zip', parsed, 'MAX', 'test')
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(AppSetting)) == 0
        record = history_import.save_history(session, raw, 'history.zip', parsed, 'MAX', 'test')
        assert record['new_message_count'] == 1
        assert session.scalar(select(func.count()).select_from(workflows.ServiceCase)) == 0


def test_checksum_gap_is_reported_instead_of_marking_file_downloaded(history_app):
    client, _, _ = staff(history_app)
    raw = bundle([message(attachments=[{'path': 'files/a.txt', 'sha256': '0' * 64}])], {'files/a.txt': b'TEST'})
    record = upload(client, raw)
    assert record['stages']['attachments_present'] == 0
    assert record['gaps'][0]['reason'] == 'sha256_mismatch'
    assert record['attachments'][0]['content_state'] == 'not_processed'


def test_one_corrupt_zip_member_does_not_discard_messages_or_healthy_files(history_app):
    client, _, _ = staff(history_app)
    bad = b'UNIQUE_TEST_ATTACHMENT_BYTES'
    raw = bundle([message(attachments=[{'path': 'files/bad.txt'}, {'path': 'files/good.txt'}])],
                 {'files/bad.txt': bad, 'files/good.txt': b'TEST: healthy original'})
    # ZipFile uses STORED here; changing just the body leaves its recorded CRC
    # invalid, while the separate manifest and healthy member remain readable.
    raw = raw.replace(bad, b'X' + bad[1:], 1)
    record = upload(client, raw)
    assert record['stages']['messages_received'] == 1
    assert record['stages']['attachments_present'] == 1
    assert record['gaps'][0]['reason'] == 'zip_member_unreadable'
    detail = client.get('/api/history/imports/' + record['id']).json()
    assert detail['stages']['attachment_contents_processed'] == 1
    assert client.get(record['raw_url']).content == raw
