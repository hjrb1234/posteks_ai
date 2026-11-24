"""Client answer use never publishes private originals or unreviewed history."""
from test_core import env, login, upload
from server import main
from server.models import AppSetting, Document
from server.source_access import can_use_for_answer, client_source_view


def test_answer_permission_is_explicit_admin_choice_and_original_stays_private(env):
    client = env.client
    login(client)
    identifier = upload(client).json()[0]['id']
    document = client.get('/api/documents').json()[0]
    assert document['answer_access'] == 'staff_only'
    client.post('/api/auth/logout')
    assert client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results'] == []
    login(client, 'operator')
    denied = client.patch(f'/api/documents/{identifier}', json={'answer_access': 'client_answers'})
    assert denied.status_code == 403
    client.post('/api/auth/logout')
    login(client)
    assert client.patch(f'/api/documents/{identifier}', json={'answer_access': 'wrong'}).status_code == 422
    changed = client.patch(f'/api/documents/{identifier}', json={'answer_access': 'client_answers'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['visibility'] == 'internal'
    assert client.get(f'/api/documents/{identifier}/file').status_code == 200
    client.post('/api/auth/logout')
    # Answer excerpts are allowed, but document lists/original pages/images are not.
    assert client.get('/api/documents').status_code == 401
    for path in ['file', 'pages/1', 'pages/1/image']:
        assert client.get(f'/api/documents/{identifier}/{path}').status_code == 404
    with env.factory() as session:
        document = session.get(Document, identifier)
        assert can_use_for_answer(document)
        metadata = client_source_view({'document_id': identifier, 'text': 'X' * 9000,
            'url': '/private-file', 'layout': {'SECRET': 'NOT_PUBLIC'}}, document)
        assert metadata['original_private'] and metadata['file_available'] is False
        assert metadata['url'] is None and len(metadata['text']) == 2200
        assert 'layout' not in metadata


def test_grant_and_revoke_affect_live_answer_history_without_publishing_file(env, monkeypatch):
    client = env.client
    login(client)
    identifier = upload(client).json()[0]['id']
    assert client.patch(f'/api/documents/{identifier}', json={'answer_access': 'client_answers'}).status_code == 200
    def answered(session, *args, **kwargs):
        return {'content': 'Разрешённая выдержка для ответа.',
                'sources': [{'document_id': identifier, 'page': 1, 'text': 'Разрешённая выдержка',
                             'url': f'/api/documents/{identifier}/file#page=1'}],
                'engine': 'injected', 'action': 'answer'}
    monkeypatch.setattr(main.ai, 'answer', answered)
    response = client.post('/api/chat', json={'message': 'PE38-12 регулировка'}).json()
    assert response['message']['content'] == 'Разрешённая выдержка для ответа.'
    source = response['message']['sources'][0]
    assert source['original_private'] and source['url'] is None
    assert source['answer_access'] == 'client_answers'
    identifier_conversation = response['conversation_id']
    assert client.patch(f'/api/documents/{identifier}', json={'answer_access': 'staff_only'}).status_code == 200
    history = client.get('/api/conversations/' + identifier_conversation).json()
    assert history['messages'][-1]['sources'] == []
    assert 'больше не доступны' in history['messages'][-1]['content']


def test_permission_withdrawn_during_generation_is_checked_fresh(env, monkeypatch):
    client = env.client
    login(client)
    identifier = upload(client).json()[0]['id']
    client.patch(f'/api/documents/{identifier}', json={'answer_access': 'client_answers'})
    held = []
    def delayed(session, *args, **kwargs):
        held.append(session.get(Document, identifier))
        with env.factory() as other:
            other.get(Document, identifier).answer_access = 'staff_only'
            other.commit()
        return {'content': 'REVOKED_PRIVATE_TEXT',
                'sources': [{'document_id': identifier, 'page': 1, 'text': 'REVOKED_PRIVATE_TEXT'}],
                'engine': 'injected', 'action': 'answer'}
    monkeypatch.setattr(main.ai, 'answer', delayed)
    reply = client.post('/api/chat', json={'message': 'PE38-12 регулировка'}).json()
    assert 'REVOKED_PRIVATE_TEXT' not in str(reply)
    assert reply['message']['sources'] == []


def test_history_cannot_be_enabled_after_category_and_metadata_rename(env):
    client = env.client
    login(client)
    identifier = upload(client).json()[0]['id']
    with env.factory() as session:
        document = session.get(Document, identifier)
        document.category = 'Обычное название'
        document.indexing_details = {}
        session.add(AppSetting(key='history_import:test-ledger', value={'document_id': identifier}))
        session.commit()
    for patch in [{'answer_access': 'client_answers'}, {'visibility': 'public'},
                  {'answer_access': 'client_answers', 'category': 'Руководство', 'models': ['PE38-12']}]:
        result = client.patch(f'/api/documents/{identifier}', json=patch)
        assert result.status_code == 409
    with env.factory() as session:
        assert not can_use_for_answer(session.get(Document, identifier))


def test_approved_private_documents_are_eligible_for_semantic_retrieval_only(env):
    from server.semantic_search import _allowed_document, _corpus, _resolve
    login(env.client)
    identifier = upload(env.client).json()[0]['id']
    with env.factory() as session:
        document = session.get(Document, identifier)
        assert not _allowed_document(document, False, set())
        document.answer_access = 'client_answers'
        session.commit()
        assert _allowed_document(document, False, set())
        corpus, _ = _corpus(session, False)
        sources = [source for source in corpus if source.get('document_id') == identifier]
        assert sources and sources[0]['original_private'] and not sources[0]['file_available']
        key = sources[0]['key']
        document.answer_access = 'staff_only'
        session.commit()
        assert _resolve(session, key, False) is None


def test_migration_retains_existing_document_access_and_requires_explicit_grant(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment.update(DATABASE_URL='sqlite:///' + str(tmp_path / 'answer-access.sqlite'),
        STORAGE_DIR=str(tmp_path / 'private-storage'), APP_MODE='TEST', COOKIE_SECURE='false',
        OPENAI_API_KEY='', POSTEKS_OPENAI_API_KEY='')
    code = '''
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from server.db import SessionLocal
from server import models, workflows
config=Config('alembic.ini')
command.upgrade(config,'e2a4b6c8d010')
with SessionLocal() as session:
    for number,visibility in [(1,'internal'),(2,'public')]:
        session.execute(text("""INSERT INTO documents
          (id,title,filename,storage_name,sha256,mime_type,size_bytes,category,models,visibility,status,
           pages,chunks,version,indexing_details,deleted,created_at)
          VALUES(:id,'Original title','manual.txt',:storage,:hash,'text/plain',5,'Документация','[]',
          :visibility,'indexed',1,1,1,'{}',0,'2026-10-29 00:00:00')"""),
          {'id':number,'storage':str(number)+'.txt','hash':str(number).zfill(64),'visibility':visibility})
    before=session.execute(text('SELECT * FROM documents ORDER BY id')).mappings().all()
    session.commit()
command.upgrade(config,'head')
command.upgrade(config,'head')
with SessionLocal() as session:
    after=session.execute(text('SELECT * FROM documents ORDER BY id')).mappings().all()
    assert [{key:row[key] for key in before[0]} for row in after] == before
    assert all(row['answer_access']=='staff_only' for row in after)
    assert session.execute(text('PRAGMA integrity_check')).scalar()=='ok'
print('Existing documents and visibility preserved; no answer permission granted automatically')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no answer permission granted automatically' in result.stdout
