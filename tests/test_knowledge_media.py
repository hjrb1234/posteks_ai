"""Real extraction, persistence, publication policy and private media boundaries."""
import io
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_core import env, login, upload
from server.config import ROOT, Settings, settings
from server.models import AppSetting, Document, DocumentPage
from server import knowledge, main


def test_pdf_index_preserves_visual_tables_and_progress_after_reindex(env):
    pdf = pymupdf.open()
    page = pdf.new_page()
    for y in (60, 90, 120, 150):
        page.draw_line((40, y), (460, y))
    for x in (40, 180, 320, 460):
        page.draw_line((x, 60), (x, 150))
    for x, y, label in [(50, 80, 'Parameter'), (190, 80, 'PE38-17'), (330, 80, 'PE38-25'),
                         (50, 110, 'Speed'), (190, 110, '17'), (330, 110, '25'),
                         (50, 140, 'Width'), (190, 140, '200'), (330, 140, '400')]:
        page.insert_text((x, y), label)
    original = pdf.tobytes()
    pdf.close()
    login(env.client)
    result = env.client.post('/api/documents', files={'files': ('PE38-17_PE38-25.pdf', original, 'application/pdf')})
    assert result.status_code == 200, result.text
    document_id = result.json()[0]['id']
    document = env.client.get('/api/documents').json()[0]
    assert document['status'] == 'indexed'
    assert document['indexing']['stage'] == 'complete' and document['indexing']['completed'] == 1
    viewed = env.client.get(f'/api/documents/{document_id}/pages/1').json()
    assert viewed['layout']['tables'] and viewed['layout']['tables'][0]['rows'][1] == ['Speed', '17', '25']
    assert len(viewed['layout']['tables'][0]['bbox']) == 4
    assert env.client.get(f'/api/documents/{document_id}/file').content == original
    assert env.client.get('/api/knowledge/search', params={'q': 'PE38-17 Speed'}).json()['results']
    env.client.post(f'/api/documents/{document_id}/reindex')
    with env.factory() as session:
        assert len(session.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).all()) == 1
    assert env.client.get(f'/api/documents/{document_id}/file').content == original


def test_publication_requires_admin_and_history_origin_cannot_be_removed(env):
    login(env.client)
    document_id = upload(env.client).json()[0]['id']
    with env.factory() as session:
        doc = session.get(Document, document_id)
        doc.category = 'История переписки (непроверено)'
        doc.indexing_details = {'source_kind': 'unreviewed_history'}
        session.commit()
    env.client.patch(f'/api/documents/{document_id}', json={'category': 'Руководство'})
    env.client.post(f'/api/documents/{document_id}/reindex')
    assert env.client.patch(f'/api/documents/{document_id}', json={'visibility': 'public'}).status_code == 409
    env.client.post('/api/auth/logout')
    login(env.client, 'operator')
    assert upload(env.client, 'public.txt', 'Instructions', 'public').status_code == 403
    assert env.client.patch(f'/api/documents/{document_id}', json={'visibility': 'public'}).status_code == 403


def test_attachment_ownership_binding_and_persistent_chat_metadata(env):
    client = env.client
    response = client.post('/api/attachments', files={'file': ('marking.txt', 'Модель PE38-12. Надпись заказчика.'.encode(), 'text/plain')})
    assert response.status_code == 200, response.text
    attached = response.json()
    assert attached['kind'] == 'document' and 'PE38-12' in attached['analysis_text']
    assert client.get(attached['url']).status_code == 200
    with TestClient(main.app) as other:
        assert other.get(attached['url']).status_code == 404
        wrong = other.post('/api/chat', json={'message': 'Что на фото?', 'attachment_ids': [attached['id']]})
        assert wrong.status_code == 404
    answer = client.post('/api/chat', json={'message': 'Моя маркировка', 'attachment_ids': [attached['id']]}).json()
    history = client.get('/api/conversations/' + answer['conversation_id']).json()
    assert history['messages'][0]['attachments'][0]['id'] == attached['id']
    wrong_chat = client.post('/api/chat', json={'message': 'Другой диалог', 'attachment_ids': [attached['id']]})
    assert wrong_chat.status_code == 404
    assert client.get('/api/documents').status_code == 401


def test_image_validation_no_fictitious_ocr_and_exif_is_removed(env, monkeypatch):
    from PIL import Image
    from server import media
    def unreadable(*args, **kwargs):
        raise knowledge.IndexingError('OCR недоступен')
    monkeypatch.setattr(media, 'extract_pages', unreadable)
    original = Image.new('RGB', (32, 32), 'white')
    exif = original.getexif()
    exif[270] = 'Private location metadata'
    buffer = io.BytesIO()
    original.save(buffer, format='JPEG', exif=exif)
    response = env.client.post('/api/attachments', files={'file': ('plate.jpg', buffer.getvalue(), 'image/jpeg')})
    assert response.status_code == 200
    data = response.json()
    assert data['engine'] == 'unreadable' and not data['text'] and data['warnings']
    with Image.open(io.BytesIO(env.client.get(data['url']).content)) as normalized:
        assert not normalized.getexif()
    assert env.client.post('/api/attachments', files={'file': ('bad.png', b'HTML<script>')}).status_code == 422
    assert env.client.post('/api/attachments', files={'file': ('macro.exe', b'MZ')}).status_code == 415


def test_audio_missing_key_is_explicit_and_does_not_fake_transcript(env):
    header = b'RIFF' + b'\x00' * 4 + b'WAVE' + b'\x00' * 20
    response = env.client.post('/api/audio/transcribe', files={'file': ('voice.wav', header, 'audio/wav')})
    assert response.status_code == 503 and 'OpenAI' in response.json()['detail']
    assert env.client.post('/api/audio/transcribe', files={'file': ('voice.wav', b'not audio')}).status_code == 422


def test_relative_paths_remain_same_when_started_from_other_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configured = Settings(storage_dir=Path('example-storage'), database_url='sqlite+aiosqlite:///./example-storage/web.db')
    assert configured.storage_dir == ROOT / 'example-storage'
    assert configured.database_url == 'sqlite:///' + str(ROOT / 'example-storage/web.db')


def test_diagnostics_explains_internal_sources_and_denies_guests(env):
    assert env.client.get('/api/diagnostics').status_code == 401
    login(env.client)
    upload(env.client)
    data = env.client.get('/api/diagnostics').json()
    assert data['counts']['documents'] == data['counts']['internal'] == 1
    assert data['counts']['indexed'] == 1 and data['counts']['public'] == 0
    assert any('AI Playground' in warning for warning in data['warnings'])
    assert 'password' not in str(data).lower() and 'api_key' not in str(data).lower()
