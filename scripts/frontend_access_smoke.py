"""Exercise source permissions and safe mail diagnostics with a real TEST server.

The citation is seeded as a saved synthetic chat message: this check validates
the reader and permission controls, not language-model answer quality.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
import pymupdf as fitz
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'artifacts' / 'answer-access-browser'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='posteks-source-access-') as directory:
        runtime = os.environ.copy()
        for name in list(runtime):
            if name.startswith(('OPENAI_', 'POSTEKS_OPENAI_', 'MAIL_', 'IMAP_', 'SMTP_', 'MAX_')) or name in {'AI_TOKEN', 'BOT_TOKEN'}:
                runtime[name] = ''
        runtime.update({name: '' for name in ('OPENAI_API_KEY', 'POSTEKS_OPENAI_API_KEY', 'AI_TOKEN',
            'MAIL_USERNAME', 'MAIL_ADDRESS', 'MAIL_APP_PASSWORD', 'MAIL_OAUTH_TOKEN', 'MAIL_IMAP_HOST',
            'IMAP_USER', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_HOST', 'BOT_TOKEN')})
        database = Path(directory) / 'posteks_ai.db'
        runtime.update(STORAGE_DIR=directory, SERVICE_STORAGE_ROOT=directory,
            DATABASE_URL='sqlite:///' + str(database), DB_URL='', APP_MODE='TEST', COOKIE_SECURE='false',
            PUBLIC_BASE_URL='', ALLOWED_ORIGINS='', MAIL_POLL_ENABLED='false')
        password = secrets.token_urlsafe(24)
        prepared = subprocess.run([sys.executable, '-m', 'scripts.manage', 'create-admin',
            '--username', 'access-check', '--password-stdin'], cwd=ROOT, env=runtime,
            input=password + '\n', text=True, capture_output=True)
        assert prepared.returncode == 0, prepared.stderr
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        checks, javascript_errors = [], []
        with (OUTPUT / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server.main:app',
                '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=runtime,
                stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    try:
                        if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None, 'TEST server exited'
                    time.sleep(.1)
                else:
                    raise RuntimeError('TEST server failed readiness')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True,
                        executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),
                        args=['--no-sandbox', '--disable-dev-shm-usage'])
                    # Pause a real auth request, without supplying a mock
                    # response. A first chat/history request must wait for the
                    # actual guest cookie instead of racing to replace it.
                    fresh = browser.new_context().new_page()
                    held_auth, bootstrap_requests = [], []
                    fresh.route('**/api/auth/me', lambda route: held_auth.append(route))
                    fresh.on('request', lambda request: bootstrap_requests.append(request.url))
                    with fresh.expect_request(lambda request: request.url.endswith('/api/auth/me')):
                        fresh.goto(base, wait_until='domcontentloaded')
                    expect(fresh.get_by_text('Подключаем сервисный чат…', exact=True)).to_be_visible()
                    assert fresh.get_by_label('Ваш вопрос', exact=True).count() == 0
                    assert not any('/api/conversations' in url for url in bootstrap_requests)
                    assert len(held_auth) == 1
                    held_auth[0].continue_()
                    fresh.unroute('**/api/auth/me')
                    fresh.get_by_label('Ваш вопрос', exact=True).fill('Здравствуйте! Нужна инструкция по оборудованию.')
                    with fresh.expect_response(lambda response: response.url.endswith('/api/chat') and response.request.method == 'POST') as first_chat:
                        fresh.get_by_role('button', name='Отправить вопрос', exact=True).click()
                    assert first_chat.value.ok, first_chat.value.text()
                    first_id = first_chat.value.json()['conversation_id']
                    assert fresh.request.get(base + '/api/conversations/' + first_id).ok
                    with sqlite3.connect(database) as connection:
                        assert connection.execute('SELECT count(*) FROM guest_sessions').fetchone()[0] == 1
                    fresh.close()
                    checks.append('Delayed real auth bootstrap gates chat/history; immediate first send retains one guest session and owned conversation')
                    staff = browser.new_context(viewport={'width': 1440, 'height': 1000}).new_page()
                    staff.on('pageerror', lambda error: javascript_errors.append(str(error)))
                    staff.on('dialog', lambda dialog: dialog.accept())
                    login = staff.request.post(base + '/api/auth/login', data={'username': 'access-check', 'password': password})
                    assert login.ok, login.text()
                    pdf = fitz.open()
                    pdf.new_page().insert_text((50, 50), 'PE38-12 TEST MANUAL. The quoted section is approved for customer support. ' * 3)
                    original = pdf.tobytes()
                    pdf.close()
                    uploaded = staff.request.post(base + '/api/documents', multipart={
                        'files': {'name': 'TEST-PE38-12-manual.pdf', 'mimeType': 'application/pdf', 'buffer': original},
                        'visibility': 'internal'})
                    assert uploaded.ok, uploaded.text()
                    document_id = uploaded.json()[0]['id']
                    staff.goto(base + '/admin/knowledge')
                    row = staff.locator('.document-row').filter(has_text='TEST-PE38-12-manual')
                    expect(row).to_contain_text('В поиске', timeout=30000)
                    expect(staff.locator('.answer-access-guide')).to_contain_text('Клиентский помощник не использует')
                    row.locator('summary').click()
                    row.get_by_role('button', name='Разрешить ответы клиентам', exact=True).click()
                    expect(row).to_contain_text('Ответы клиентам разрешены')
                    record = next(item for item in staff.request.get(base + '/api/documents').json() if item['id'] == document_id)
                    assert record['visibility'] == 'internal' and record['answer_access'] == 'client_answers'
                    row.locator('.doc-select').click()
                    expect(staff.locator('.source-drawer')).to_contain_text('TEST MANUAL')
                    link = staff.get_by_role('link', name='Открыть оригинал')
                    assert staff.request.get(base + link.get_attribute('href').split('#')[0]).body() == original
                    staff.get_by_role('button', name='Закрыть источник', exact=True).click()
                    staff.screenshot(path=str(OUTPUT / 'staff-answer-access.png'), full_page=False)
                    checks.append('Admin enables answer fragments without publishing PDF; staff original bytes remain exact')

                    guest = browser.new_context(viewport={'width': 1200, 'height': 900}).new_page()
                    guest.on('pageerror', lambda error: javascript_errors.append(str(error)))
                    guest.goto(base)
                    assert guest.request.get(base + f'/api/documents/{document_id}/file').status == 404
                    assert guest.request.get(base + f'/api/documents/{document_id}/pages/1').status == 404
                    conversation = guest.request.post(base + '/api/chat', data={'message': 'Здравствуйте!'})
                    assert conversation.ok, conversation.text()
                    conversation_id = conversation.json()['conversation_id']
                    excerpt = 'PE38-12 TEST MANUAL. The quoted section is approved for customer support.'
                    source = {'document_id': document_id, 'page': 1, 'title': 'TEST-PE38-12-manual',
                        'source_type': 'document', 'text': excerpt, 'excerpt': excerpt,
                        'original_private': True, 'file_available': False, 'url': None}
                    with sqlite3.connect(database) as connection:
                        connection.execute("UPDATE chat_messages SET content=?,sources=? WHERE conversation_id=? AND role='assistant'",
                            ('Ответ по разрешённому фрагменту [1].', json.dumps([source]), conversation_id))
                    seeded = guest.request.get(base + '/api/conversations/' + conversation_id)
                    assert seeded.ok and seeded.json()['messages'][-1]['sources'], seeded.text()
                    guest.goto(base + '/chat/' + conversation_id)
                    expect(guest.locator('.source-card')).to_have_count(1)
                    requests = []
                    guest.on('request', lambda request: requests.append(request.url))
                    guest.locator('.source-card').click()
                    expect(guest.locator('.source-drawer')).to_contain_text(excerpt)
                    expect(guest.locator('.source-private-notice')).to_contain_text('Оригинал документа доступен только сотрудникам')
                    assert guest.locator('.source-drawer a').count() == 0
                    assert not any('/api/documents/' in url for url in requests), requests
                    guest.screenshot(path=str(OUTPUT / 'guest-approved-excerpt.png'))
                    guest.get_by_role('button', name='Закрыть источник', exact=True).click()
                    checks.append('Guest saved citation displays only provided excerpt and never requests protected page/image/PDF')

                    if row.locator('.document-menu').get_attribute('open') is None:
                        row.locator('summary').click()
                    row.get_by_role('button', name='Запретить ответы клиентам', exact=True).click()
                    expect(row).not_to_contain_text('Ответы клиентам разрешены')
                    history = guest.request.get(base + '/api/conversations/' + conversation_id).json()
                    assert excerpt not in str(history) and history['messages'][-1]['sources'] == []
                    checks.append('Revoking answer permission removes the saved public citation on fresh history read')

                    public = staff.request.patch(base + f'/api/documents/{document_id}', data={'visibility': 'public'})
                    assert public.ok, public.text()
                    staff.reload()
                    expect(row).to_contain_text('Публичный оригинал')
                    expect(row).to_contain_text('Ответы клиентам разрешены')
                    row.locator('summary').click()
                    assert row.get_by_role('button', name='Запретить ответы клиентам', exact=True).count() == 0
                    assert row.get_by_role('button', name='Разрешить ответы клиентам', exact=True).count() == 0
                    row.get_by_role('button', name='Закрыть оригинал', exact=True).click()
                    expect(row).to_contain_text('Внутренний оригинал')
                    expect(row).not_to_contain_text('Ответы клиентам разрешены')
                    assert guest.request.get(base + f'/api/documents/{document_id}/file').status == 404
                    checks.append('Legacy public original is honestly marked answerable; UI requires closing it before private answer controls apply')

                    seed = '''from server.main import app
from server.db import SessionLocal,utcnow
from server.mail_models import MailSyncRun
from server.mail_sync import account_key
with SessionLocal() as session:
    session.add(MailSyncRun(account_key=account_key(),kind='history',status='failed',days=365,batch_size=100,
        fetched=0,imported=0,duplicates=0,errors=1,error_code='imap_fetch_failed',
        error='Не удалось получить часть письма (UID 32, ответ NO). Контрольная точка не продвинута.',
        progress=[{'folder':'INBOX','failed_attempts':1,'failure_detail':{'uid':32,'stage':'body_partial','imap_status':'NO','response_code':'UNAVAILABLE'}}],finished_at=utcnow()))
    session.commit()
'''
                    result = subprocess.run([sys.executable, '-c', seed], cwd=ROOT, env=runtime, capture_output=True, text=True)
                    assert result.returncode == 0, result.stderr
                    staff.goto(base + '/admin/inbox')
                    staff.get_by_role('button', name='Показать журнал и контрольные точки').click()
                    expect(staff.locator('.mail-run-list')).to_contain_text('UID 32, ответ NO')
                    staff.get_by_text('Диагностика загрузки · папок с замечаниями:', exact=False).click()
                    expect(staff.locator('.mail-fetch-diagnostics')).to_contain_text('Чтение части письма')
                    expect(staff.locator('.mail-fetch-diagnostics')).to_contain_text('UID: 32')
                    expect(staff.locator('.mail-run-list')).to_contain_text('Ошибок обработки: 1')
                    checks.append('Mail failure preserves safe UID/stage/status diagnostics and integer error count')
                    browser.close()
                assert not javascript_errors, javascript_errors
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        report = {'status': 'passed', 'checks': checks, 'javascript_errors': javascript_errors,
                  'note': 'Linux Chromium and real isolated TEST backend; seeded saved citation tests reader permissions, not LLM answer quality.'}
        (OUTPUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
