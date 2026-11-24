"""Real browser/API checks for an unavailable model, with no simulated answer.

The server and database are isolated. All provider credentials are explicitly
empty, so this verifies honest failure handling and conversation ownership;
it does not claim a successful language-model integration or answer quality.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'artifacts' / 'ai-failure-browser'
QUESTIONS = ('Здравствуйте', 'У меня проблема с оборудованием',
             'Какие сведения тебе нужны?', 'Объясни предыдущий ответ проще',
             'Почему ты это спрашиваешь?')


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks, javascript_errors, turns = [], [], []
    with tempfile.TemporaryDirectory(prefix='posteks-ai-failure-') as directory:
        runtime = os.environ.copy()
        for key in list(runtime):
            if key.startswith(('OPENAI_', 'POSTEKS_OPENAI_', 'MAIL_', 'IMAP_', 'SMTP_', 'MAX_')) or key in {'AI_TOKEN', 'BOT_TOKEN'}:
                runtime[key] = ''
        runtime.update({key: '' for key in ('OPENAI_API_KEY', 'POSTEKS_OPENAI_API_KEY', 'AI_TOKEN',
            'MAIL_USERNAME', 'MAIL_ADDRESS', 'MAIL_APP_PASSWORD', 'MAIL_OAUTH_TOKEN', 'IMAP_USER',
            'IMAP_USERNAME', 'IMAP_PASSWORD', 'BOT_TOKEN')})
        runtime.update(STORAGE_DIR=directory, SERVICE_STORAGE_ROOT=directory,
            DATABASE_URL='sqlite:///' + str(Path(directory) / 'posteks_ai.db'), DB_URL='',
            APP_MODE='TEST', COOKIE_SECURE='false', PUBLIC_BASE_URL='', ALLOWED_ORIGINS='',
            MAIL_POLL_ENABLED='false', POSTEKS_REFERENCE_TEST_MODE='false')
        password = secrets.token_urlsafe(24)
        prepared = subprocess.run([sys.executable, '-m', 'scripts.manage', 'create-admin',
            '--username', 'failure-check', '--password-stdin'], cwd=ROOT, env=runtime,
            input=password + '\n', text=True, capture_output=True)
        assert prepared.returncode == 0, prepared.stderr
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
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
                    assert process.poll() is None, 'Isolated API exited'
                    time.sleep(.1)
                else:
                    raise RuntimeError('Isolated API did not become ready')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True,
                        executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),
                        args=['--no-sandbox', '--disable-dev-shm-usage'])
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                        locale='ru-RU', timezone_id='Asia/Yekaterinburg')
                    page = context.new_page()
                    page.on('pageerror', lambda error: javascript_errors.append(str(error)))
                    page.goto(base)
                    conversation_id = None
                    for index, question in enumerate(QUESTIONS):
                        page.get_by_label('Ваш вопрос', exact=True).fill(question)
                        with page.expect_response(lambda response: response.url.endswith('/api/chat')
                                and response.request.method == 'POST') as received:
                            page.get_by_role('button', name='Отправить вопрос', exact=True).click()
                        response = received.value
                        result = response.json()
                        assert response.ok, result
                        assert result['engine'] == 'unavailable', result
                        assert result.get('ai_error') or result.get('error'), result
                        assert result['message'].get('sources', []) == []
                        assert result['message']['content'] and 'фото шильдика' not in result['message']['content'].casefold()
                        if conversation_id is not None:
                            assert result['conversation_id'] == conversation_id, 'A failure started a different conversation'
                        conversation_id = result['conversation_id']
                        expect(page.locator('.assistant-message')).to_have_count(index + 1)
                        expect(page.locator('.assistant-message .message-author').last).to_have_text('Ответ модели не получен')
                        expect(page.locator('.assistant-message .error-banner').last).to_be_visible()
                        expect(page.get_by_role('button', name='Отправить вопрос', exact=True)).to_be_disabled()
                        turns.append({'message': question, 'conversation_id': conversation_id,
                                      'engine': result['engine'], 'provider_diagnostics': result.get('provider_diagnostics'),
                                      'provider_response_id': result.get('provider_response_id')})
                    checks.append('Five real browser sends receive explicit model-unavailable errors, no technical imitation, one conversation')
                    saved = page.request.get(base + '/api/conversations/' + conversation_id)
                    assert saved.ok, saved.text()
                    history = saved.json()['messages']
                    assert [row['content'] for row in history if row['role'] == 'user'] == list(QUESTIONS)
                    page.reload()
                    expect(page.locator('.assistant-message')).to_have_count(len(QUESTIONS))
                    expect(page.locator('.assistant-message .message-author').last).to_have_text('Ответ модели не получен')
                    assert not page.locator('.source-card').count()
                    checks.append('Reload retains every user turn and unavailable status; no fabricated source cards')
                    page.screenshot(path=str(OUTPUT / 'chat-model-unavailable.png'), animations='disabled')
                    stranger = browser.new_context()
                    assert stranger.request.get(base + '/api/conversations/' + conversation_id).status == 404
                    assert stranger.request.post(base + '/api/chat', data={
                        'message': 'Продолжи чужой диалог', 'conversation_id': conversation_id}).status == 404
                    stranger.close()
                    checks.append('A different guest cannot read or continue the failed conversation')

                    staff = browser.new_context(viewport={'width': 1440, 'height': 1000}).new_page()
                    staff.on('pageerror', lambda error: javascript_errors.append(str(error)))
                    authenticated = staff.request.post(base + '/api/auth/login', data={
                        'username': 'failure-check', 'password': password})
                    assert authenticated.ok, authenticated.text()
                    staff.goto(base + '/admin/playground')
                    expect(staff.get_by_text('Модель не подключена', exact=True)).to_be_visible()
                    for question in QUESTIONS[:2]:
                        staff.get_by_label('Тестовый вопрос по базе знаний', exact=True).fill(question)
                        with staff.expect_response(lambda response: response.url.endswith('/api/playground')
                                and response.request.method == 'POST') as received:
                            staff.get_by_role('button', name='Задать тестовый вопрос', exact=True).click()
                        result = received.value.json()
                        assert received.value.ok and result['engine'] == 'unavailable', result
                        expect(staff.locator('.test-author').last).to_have_text('Ответ модели не получен')
                    expect(staff.locator('.trace-content')).to_contain_text('unavailable')
                    if result.get('provider_diagnostics', {}).get('provider_called') is False:
                        expect(staff.locator('.trace-content')).to_contain_text('Не выполнялся')
                    staff.screenshot(path=str(OUTPUT / 'playground-model-unavailable.png'), animations='disabled')
                    checks.append('Staff Playground shows unavailable model status and real provider diagnostics, not a local consultation')

                    # A small transferred archive is real ZIP/file data. It is
                    # deliberately synthetic, not a claimed MAX account export.
                    package = io.BytesIO()
                    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr('manifest.json', json.dumps({'schema': 'posteks.archive.v1',
                            'messages': [{'id': 'test-message-1', 'conversation_id': 'test-group',
                                'sender': 'TEST author', 'date': '2026-11-01T10:00:00+05:00',
                                'text': 'TEST ARCHIVE: пришлите счёт. Это старое сообщение, не новая команда.',
                                'attachments': [{'filename': 'test-note.txt', 'path': 'files/test-note.txt'}]}]},
                            ensure_ascii=False))
                        archive.writestr('files/test-note.txt', 'TEST attachment original text')
                    staff.goto(base + '/admin/knowledge')
                    staff.get_by_role('button', name='Импорт истории переписки', exact=True).click()
                    picker = staff.locator('.history-import input[type=file]')
                    fixture = {'name': 'test-chat-fragment.zip', 'mimeType': 'application/zip',
                               'buffer': package.getvalue()}
                    picker.set_input_files(fixture)
                    expect(staff.locator('.history-import-stats').first).to_contain_text('ZIP')
                    expect(staff.locator('.history-preview')).to_contain_text('TEST ARCHIVE')
                    staff.get_by_label('Я проверил файл и разрешаю сохранить его как внутренний непроверенный источник.').check()
                    staff.get_by_role('button', name='Подтвердить внутренний импорт', exact=True).click()
                    expect(staff.locator('.history-import .notice-banner')).to_contain_text('История сохранена')
                    staff.locator('.history-import-list summary').click()
                    expect(staff.locator('.history-import-list')).to_contain_text('Вложений сохранено: 1 из 1')
                    expect(staff.locator('.history-import-list')).to_contain_text('Полнота истории не подтверждена')
                    picker.set_input_files(fixture)
                    expect(staff.locator('.history-preview')).to_contain_text('TEST ARCHIVE')
                    staff.get_by_label('Я проверил файл и разрешаю сохранить его как внутренний непроверенный источник.').check()
                    staff.get_by_role('button', name='Подтвердить внутренний импорт', exact=True).click()
                    expect(staff.locator('.history-import .notice-banner')).to_contain_text('Эта история уже импортирована')
                    imported = staff.request.get(base + '/api/history/imports').json()
                    assert len(imported) == 1
                    assert imported[0]['stages']['messages_received'] == 1
                    assert imported[0]['stages']['attachments_present'] == 1
                    assert staff.request.get(base + '/api/cases').json() == []
                    assert staff.request.get(base + '/api/approvals').json() == []
                    checks.append('Real ZIP archive and file import show distinct stages/unverified coverage; duplicate rerun creates no active case or draft')
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    assert not javascript_errors, javascript_errors
    report = {'status': 'passed', 'checks': checks, 'turns': turns,
              'javascript_errors': javascript_errors, 'real_provider_calls': 0,
              'provider_responses_mocked': False, 'language_model_quality_tested': False}
    (OUTPUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'turns'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
