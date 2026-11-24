"""Real browser smoke checks, always in an isolated TEST database.

Run from repo root: .venv/bin/python -m scripts.browser_smoke
Requires playwright and Chromium (CHROMIUM_PATH or a Playwright installation).
No real mail or OpenAI credentials are used. Creates screenshots under artifacts/browser.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path(os.environ.get('POSTEKS_BROWSER_OUTPUT', str(ROOT / 'artifacts' / 'browser')))


def main():
    if not (ROOT / 'frontend' / 'dist' / 'index.html').is_file():
        raise RuntimeError('Сначала соберите frontend')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='posteks-browser-') as temporary:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(('OPENAI_', 'POSTEKS_OPENAI_', 'MAIL_', 'IMAP_', 'SMTP_')):
                env.pop(key)
        env.update(STORAGE_DIR=temporary, DATABASE_URL='sqlite:///' + str(Path(temporary) / 'posteks_ai.db'),
                   APP_MODE='TEST', POSTEKS_REFERENCE_TEST_MODE='true', OPENAI_API_KEY='', POSTEKS_OPENAI_API_KEY='',
                   TESSDATA_PREFIX=str(ROOT / 'storage' / 'tessdata'), COOKIE_SECURE='false')
        password = secrets.token_urlsafe(24)
        result = subprocess.run([sys.executable, '-m', 'scripts.manage', 'create-admin', '--username', 'browser-test', '--password-stdin'],
                                cwd=ROOT, env=env, input=password + '\n', text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('Не удалось подготовить изолированного администратора: ' + result.stderr)
        # Real attached manuals are optional in a source-only clone, required when present.
        sources = ROOT / 'storage' / 'imports'
        if sources.is_dir() and '--with-private-sources' in sys.argv:
            result = subprocess.run([sys.executable, '-m', 'scripts.manage', 'import-sources', '--directory', str(sources)],
                                    cwd=ROOT, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('Ошибка индексации тестовых копий реальных документов: ' + result.stderr)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with (OUTPUT / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    try:
                        if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        raise RuntimeError('Тестовый сервер завершился при старте')
                    time.sleep(.2)
                else:
                    raise RuntimeError('Тестовый сервер не готов')
                with sync_playwright() as playwright:
                    executable = os.environ.get('CHROMIUM_PATH') or shutil.which('chromium')
                    launch = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
                    if executable:
                        launch['executable_path'] = executable
                    browser = playwright.chromium.launch(**launch)
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='ru-RU', timezone_id='Asia/Yekaterinburg')
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('dialog', lambda dialog: dialog.accept())
                    page.goto(base)
                    expect(page.get_by_role('heading', name='Сервис рядом. Решение — ближе.')).to_be_visible()
                    page.screenshot(path=str(OUTPUT / 'chat-desktop.png'), full_page=True)
                    assert page.request.get(base + '/api/documents').status == 401
                    page.get_by_label('Ваш вопрос', exact=True).fill('Как изменить размеры короба PE38-12?')
                    with page.expect_response(lambda res: res.url.endswith('/api/chat') and res.request.method == 'POST') as answered:
                        page.get_by_role('button', name='Отправить вопрос', exact=True).click()
                    payload = answered.value.json()
                    assert answered.value.status == 200 and payload['engine'] == 'search_only'
                    assert payload['message']['sources'] == []
                    expect(page.locator('.assistant-message')).to_have_count(1)
                    expect(page.locator('.assistant-message')).to_contain_text(payload['message']['content'][:60])
                    assert not page.locator('.source-card').count(), 'Internal sources leaked into public chat'
                    checks.append('Публичный чат: реальный запрос, честный поиск без AI, закрытые источники исключены')

                    page.get_by_role('button', name='Рабочее пространство Вход для сотрудников').click()
                    page.get_by_label('Логин', exact=True).fill('browser-test')
                    page.get_by_label('Пароль', exact=True).fill(password)
                    page.get_by_role('button', name='Войти в пространство').click()
                    expect(page).to_have_url(base + '/admin/inbox')
                    checks.append('Вход сотрудника через UI и серверную сессию')

                    page.goto(base + '/admin/knowledge')
                    expect(page.get_by_role('heading', name=re.compile('^База знаний'))).to_be_visible()
                    documents = page.request.get(base + '/api/documents').json()
                    if documents:
                        assert len(documents) == 6 and sum(d['pages'] for d in documents) == 354
                        assert all(d['status'] == 'indexed' and d['visibility'] == 'internal' for d in documents)
                        expect(page.locator('.document-row')).to_have_count(6)
                        page.screenshot(path=str(OUTPUT / 'knowledge-desktop.png'), full_page=True)
                        question = page.get_by_label('Тестовый вопрос по базе знаний')
                        question.fill('PE38-12: не могу найти настройку размеров короба в меню')
                        page.get_by_role('button', name='Задать тестовый вопрос').click()
                        expect(page.locator('.source-card').first).to_be_visible()
                        assert not page.locator('.test-message.assistant').get_by_text('PE38-17', exact=True).count()
                        page.locator('.source-card').first.click()
                        expect(page.get_by_role('dialog', name='Источник ответа')).to_be_visible()
                        expect(page.locator('.source-preview img')).to_be_visible()
                        page.wait_for_function("document.querySelector('.source-preview img')?.naturalWidth > 0")
                        expect(page.locator('.source-text')).not_to_contain_text('Загрузка текста…')
                        page.screenshot(path=str(OUTPUT / 'source-page.png'), full_page=True)
                        page.get_by_role('button', name='Закрыть источник', exact=True).click()
                        checks.append('Все6 PDF/354страницы: внутренний поиск PE38-12, цитата и реальная иллюстрированная страница')

                    page.locator('.knowledge-main input[type=file]').set_input_files({'name': 'browser-test.txt', 'mimeType': 'text/plain', 'buffer': 'ТЕСТОВЫЙ ДОКУМЕНТ. PE38-12. Проверка загрузки нового файла.'.encode()})
                    expect(page.get_by_text('ТЕСТОВЫЙ ДОКУМЕНТ', exact=False)).to_have_count(0)
                    expect(page.locator('.document-row')).to_have_count(len(documents) + 1)
                    uploaded_row = page.locator('.document-row').filter(has_text='browser-test')
                    uploaded_row.locator('.doc-select').click()
                    expect(page.locator('.source-text')).to_contain_text('ТЕСТОВЫЙ ДОКУМЕНТ')
                    assert page.locator('.source-preview img').count() == 0, 'Text sources must not request PDF previews'
                    page.get_by_role('button', name='Закрыть источник', exact=True).click()
                    uploaded_row.locator('summary').click()
                    uploaded_row.get_by_role('button', name='Открыть оригинал посетителям', exact=True).click()
                    expect(uploaded_row).to_contain_text('Публичный оригинал')
                    checks.append('Загрузка и просмотр TXT без ошибочного PDF-запроса; отдельная публикация подтверждена через UI')

                    page.goto(base + '/admin/cases')
                    page.get_by_role('button', name='Новое обращение', exact=True).click()
                    page.get_by_label('Тема обращения').fill('[TEST] Настройка формирователя')
                    page.get_by_label('Клиент / организация').fill('Тестовая организация')
                    page.get_by_label('Модель оборудования').fill('PE38-12')
                    page.get_by_label('Описание', exact=True).fill('ТЕСТ. Требуется найти раздел руководства, реальная отправка запрещена.')
                    page.get_by_role('button', name='Создать обращение', exact=True).click()
                    expect(page.get_by_text('Обращение создано', exact=True)).to_be_visible()
                    page.get_by_label('Внутренняя заметка').fill('ТЕСТОВАЯ заметка сервисного инженера')
                    page.get_by_role('button', name='Добавить заметку').click()
                    expect(page.get_by_text('ТЕСТОВАЯ заметка сервисного инженера', exact=True)).to_be_visible()
                    page.get_by_label('Новое поручение').fill('ТЕСТ: проверить раздел инструкции')
                    page.get_by_label('Отдел-исполнитель', exact=True).fill('КБ')
                    page.get_by_role('button', name='Создать поручение', exact=True).click()
                    expect(page.get_by_text('ТЕСТ: проверить раздел инструкции', exact=True)).to_be_visible()
                    checks.append('Обращение, внутренняя заметка и поручение созданы через UI без выдуманного СО')

                    page.goto(base + '/admin/inbox')
                    page.locator('input[type=file]').set_input_files(str(ROOT / 'tests' / 'fixtures' / 'service-request.eml'))
                    expect(page.locator('.ws-company-row')).to_have_count(1)
                    page.get_by_role('button', name='По письмам', exact=True).click()
                    expect(page.locator('.ws-email-row')).to_have_count(1)
                    page.locator('.ws-email-row').first.click()
                    expect(page.locator('.ws-mail-text')).to_contain_text('ТЕСТОВОЕ ПИСЬМО')
                    page.get_by_label('Текст ответа', exact=True).fill('ТЕСТОВЫЙ ЧЕРНОВИК. Просим уточнить заводской номер и модификацию оборудования.')
                    page.get_by_role('button', name='На согласование', exact=True).click()
                    expect(page.get_by_text('Черновик сохранён и передан на проверку сотруднику. Письмо не отправлено.', exact=True)).to_be_visible()
                    page.screenshot(path=str(OUTPUT / 'inbox-desktop.png'), full_page=True)
                    page.goto(base + '/admin/approvals')
                    page.locator('.ws-select-row').first.click()
                    page.get_by_role('button', name='Согласовать', exact=True).click()
                    expect(page.get_by_text('Черновик согласован. Отправка не выполнялась.', exact=True)).to_be_visible()
                    approvals = page.request.get(base + '/api/approvals').json()
                    assert approvals[0]['status'] == 'APPROVED'
                    assert page.request.post(base + f"/api/approvals/{approvals[0]['id']}/send").status == 409
                    checks.append('Тестовый EML → черновик → согласование; сервер блокирует отправку409')

                    page.goto(base + '/admin/equipment')
                    if documents:
                        expect(page.locator('tbody tr')).to_have_count(272)
                    page.goto(base + '/admin/settings')
                    expect(page.get_by_role('heading', name=re.compile('^Настройки'))).to_be_visible()
                    page.get_by_role('button', name='Тёмная').click()
                    assert page.locator('html').get_attribute('data-theme') == 'dark'
                    page.screenshot(path=str(OUTPUT / 'settings-dark.png'), full_page=True)
                    checks.append('Реестр оборудования и переключение темы')
                    for slug in ('service', 'support'):
                        response = page.request.get(base + '/' + slug)
                        assert response.status == 200 and '<h1>' in response.text()
                    for path in ('/admin/knowledge', '/api/cases'):
                        assert 'noindex' in page.request.get(base + path).headers.get('x-robots-tag', '')
                    checks.append('Публичные HTML-страницы и запрет индексации закрытых разделов')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    for staff_path in ('knowledge', 'inbox', 'cases', 'settings'):
                        page.goto(base + '/admin/' + staff_path)
                        expect(page.locator('.workspace-topbar')).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Staff mobile overflow: ' + staff_path
                    page.goto(base + '/admin/knowledge')
                    expect(page.locator('.document-row')).to_have_count(len(documents) + 1)
                    page.screenshot(path=str(OUTPUT / 'knowledge-mobile-dark.png'), full_page=True)
                    checks.append('Мобильные закрытые разделы390px: знания, входящие, обращения, настройки без переполнения')

                    mobile = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, locale='ru-RU')
                    mobile_page = mobile.new_page()
                    mobile_page.on('pageerror', lambda error: errors.append(str(error)))
                    mobile_page.goto(base)
                    expect(mobile_page.get_by_role('heading', name='Сервис рядом. Решение — ближе.')).to_be_visible()
                    assert mobile_page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
                    mobile_page.get_by_role('button', name='Открыть меню').click()
                    expect(mobile_page.get_by_role('button', name='Новый диалог')).to_be_visible()
                    mobile_page.get_by_role('button', name='Новый диалог').click()
                    mobile_page.screenshot(path=str(OUTPUT / 'chat-mobile.png'), full_page=True)
                    checks.append('Мобильный экран390px: без горизонтальной прокрутки, меню работает')
                    assert not errors, 'Browser runtime errors: ' + '; '.join(errors)
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    report = {'status': 'passed', 'checks': checks, 'browser_errors': errors,
              'note': 'Isolated TEST database. No real mail/OpenAI or original posteks.db used.'}
    (OUTPUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
