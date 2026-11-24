"""An optional OCR failure must not destroy the usable part of a manual."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pymupdf
import pytest
from sqlalchemy import select

from test_core import env, login, upload
from server import knowledge, ocr
from server.models import Document, DocumentPage


def pdf_with_scan(*, text_pages=True):
    image = Image.new('RGB', (80, 80), 'white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    pdf = pymupdf.open()
    if text_pages:
        page = pdf.new_page()
        page.insert_text((40, 40), 'PE38-12 conveyor adjustment instructions with a complete searchable text layer.')
    page = pdf.new_page()
    page.insert_image(pymupdf.Rect(30, 30, 400, 400), stream=buffer.getvalue())
    if text_pages:
        page = pdf.new_page()
        page.insert_text((40, 40), 'PE38-12 maintenance instructions. Keep the guards closed during operation.')
    raw = pdf.tobytes()
    pdf.close()
    return raw


def unavailable(*args, **kwargs):
    raise knowledge.IndexingError('Tesseract OCR не найден. Укажите TESSERACT_CMD.', 'binary_missing')


def test_mixed_pdf_retains_text_pages_and_reports_unreadable_scan(env, monkeypatch):
    monkeypatch.setattr(knowledge, '_ocr_image', unavailable)
    login(env.client)
    original = pdf_with_scan()
    response = env.client.post('/api/documents', files={'files': ('PE38-12.pdf', original, 'application/pdf')})
    assert response.status_code == 200, response.text
    doc_id = response.json()[0]['id']
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'indexed' and doc['pages'] == 3 and doc['chunks'] >= 2
    assert doc['indexing']['extraction_complete'] is False
    assert doc['indexing']['unread_pages'] == [2]
    assert doc['indexing']['warnings'][0]['code'] == 'binary_missing'
    assert doc['indexing']['pages_with_text'] == 2
    assert env.client.get('/api/knowledge/search', params={'q': 'PE38-12 conveyor'}).json()['results']
    page = env.client.get(f'/api/documents/{doc_id}/pages/2').json()
    assert page['layout']['ocr_status'] == 'unavailable' and page['text'] == ''
    assert env.client.get(f'/api/documents/{doc_id}/pages/2/image').status_code == 200
    assert hashlib.sha256(env.client.get(f'/api/documents/{doc_id}/file').content).digest() == hashlib.sha256(original).digest()


def test_full_scan_with_no_ocr_is_not_reported_as_success(env, monkeypatch):
    monkeypatch.setattr(knowledge, '_ocr_image', unavailable)
    login(env.client)
    env.client.post('/api/documents', files={'files': ('scan.pdf', pdf_with_scan(text_pages=False), 'application/pdf')})
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'error' and doc['pages'] == 1 and not doc['chunks']
    assert 'TESSERACT_CMD' in doc['error'] and doc['indexing']['unread_pages'] == [1]
    assert doc['indexing']['extraction_complete'] is False


def test_retry_after_ocr_repair_replaces_warning_without_losing_original(env, monkeypatch):
    monkeypatch.setattr(knowledge, '_ocr_image', unavailable)
    login(env.client)
    original = pdf_with_scan()
    doc_id = env.client.post('/api/documents', files={'files': ('PE38-12.pdf', original, 'application/pdf')}).json()[0]['id']
    monkeypatch.setattr(knowledge, '_ocr_image', lambda image: 'PE38-12 recognized scan with a verified readable diagram caption.')
    assert env.client.post(f'/api/documents/{doc_id}/reindex').status_code == 200
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'indexed' and doc['pages'] == 3
    assert doc['indexing']['extraction_complete'] is True and doc['indexing']['warnings'] == []
    assert doc['indexing']['pages_with_text'] == 3
    assert env.client.get(f'/api/documents/{doc_id}/file').content == original


def test_failed_reindex_preserves_existing_search_index(env, monkeypatch):
    login(env.client)
    doc_id = upload(env.client).json()[0]['id']
    before = env.client.get('/api/documents').json()[0]
    def failed(*args, **kwargs):
        raise knowledge.IndexingError('Обработчик временно недоступен')
    monkeypatch.setattr(knowledge, 'extract_pages', failed)
    env.client.post(f'/api/documents/{doc_id}/reindex')
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'indexed' and doc['searchable'] is True and not doc['error']
    assert doc['chunks'] == before['chunks'] and doc['pages'] == before['pages']
    assert doc['indexing']['index_preserved'] is True and doc['indexing']['stage'] == 'reindex_failed'
    assert 'недоступен' in doc['indexing']['last_error']
    assert env.client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results']


def test_partial_reindex_keeps_old_ocr_text_for_same_original(env, monkeypatch):
    monkeypatch.setattr(knowledge, '_ocr_image', lambda image: 'PE38-12 original scan caption retained for conveyor maintenance.')
    login(env.client)
    doc_id = env.client.post('/api/documents', files={'files': ('PE38-12.pdf', pdf_with_scan(), 'application/pdf')}).json()[0]['id']
    monkeypatch.setattr(knowledge, '_ocr_image', unavailable)
    env.client.post(f'/api/documents/{doc_id}/reindex')
    doc = env.client.get('/api/documents').json()[0]
    page = env.client.get(f'/api/documents/{doc_id}/pages/2').json()
    assert 'original scan caption' in page['text']
    assert page['layout']['previous_text_preserved'] is True
    assert doc['indexing']['index_preserved'] is True
    assert doc['indexing']['unread_pages'] == []
    assert doc['indexing']['extraction_complete'] is False


def test_windows_binary_discovery_and_language_check_are_not_cached(tmp_path, monkeypatch):
    directory = tmp_path / 'Program Files' / 'Tesseract-OCR'
    directory.mkdir(parents=True)
    binary = directory / 'tesseract.exe'
    binary.touch()
    data = tmp_path / 'Language data'
    data.mkdir()
    monkeypatch.delenv('TESSERACT_CMD', raising=False)
    monkeypatch.setenv('PROGRAMFILES', str(directory.parent))
    monkeypatch.setenv('TESSDATA_PREFIX', str(data))
    monkeypatch.setattr(ocr.shutil, 'which', lambda value: None)
    calls = []
    result_languages = ['eng']
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=('List of available languages:\n' + '\n'.join(result_languages) + '\n').encode())
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    assert ocr.tesseract_binary() == str(binary)
    first = ocr.diagnostic()
    assert first['available'] is False and first['code'] == 'languages_missing'
    result_languages.append('rus')
    second = ocr.diagnostic()
    assert second['available'] is True and second['languages'] == ['rus', 'eng']
    assert len(calls) == 2 and calls[0][0] == [str(binary), '--list-langs']
    assert calls[0][1]['env']['TESSDATA_PREFIX'] == str(data)


def test_invalid_configured_binary_has_actionable_configuration_error(monkeypatch):
    monkeypatch.setenv('TESSERACT_CMD', 'missing-tesseract-test-executable')
    monkeypatch.setattr(ocr.shutil, 'which', lambda value: None)
    result = ocr.diagnostic()
    assert result['available'] is False and result['code'] == 'binary_configuration'
    assert 'TESSERACT_CMD' in result['error']


def test_ocr_timeout_keeps_mixed_pdf_searchable(env, monkeypatch):
    def timeout(*args, **kwargs):
        raise knowledge.IndexingError('OCR превысил 20 секунд', 'ocr_timeout')
    monkeypatch.setattr(knowledge, '_ocr_image', timeout)
    login(env.client)
    env.client.post('/api/documents', files={'files': ('PE38-12.pdf', pdf_with_scan(), 'application/pdf')})
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'indexed' and doc['pages'] == 3
    assert doc['indexing']['warnings'][0]['code'] == 'ocr_timeout'


def test_unexpected_page_ocr_failure_still_retains_other_pdf_pages(env, monkeypatch):
    def broken_render(*args, **kwargs):
        raise ValueError('Synthetic image renderer failure')
    monkeypatch.setattr(knowledge, '_ocr_image', broken_render)
    login(env.client)
    env.client.post('/api/documents', files={'files': ('PE38-12.pdf', pdf_with_scan(), 'application/pdf')})
    doc = env.client.get('/api/documents').json()[0]
    assert doc['status'] == 'indexed' and doc['pages'] == 3
    assert doc['indexing']['warnings'][0]['code'] == 'ocr_render_failed'
    assert 'Synthetic' not in str(doc)


def test_missing_data_folder_is_distinct_from_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, 'tesseract_binary', lambda: '/synthetic/tesseract')
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'missing-data'))
    result = ocr.diagnostic()
    assert result['available'] is False and result['code'] == 'data_missing'
    assert 'TESSDATA_PREFIX' in result['error']
