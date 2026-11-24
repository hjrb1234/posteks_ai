"""Exercise the 2.0 interface against an isolated real TEST backend.

No mock API transport, real credentials, company documents or working database
are used. Browser scale checks emulate Windows display scaling with viewport and
device scale factor; they do not claim execution on Windows itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from email.message import EmailMessage
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path(os.environ.get('POSTEKS_V2_BROWSER_OUTPUT', str(ROOT / 'artifacts' / 'v2')))


def _no_overflow(page, label):
    dimensions = page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
    assert dimensions['scroll'] <= dimensions['width'] + 1, f'{label}: {dimensions}'


def _contrast(page, selector, theme):
    values = page.locator(selector).first.evaluate('''element => {
        const color = value => {
            const numbers = value.match(/[\\d.]+/g)?.map(Number) || [];
            return [...numbers.slice(0, 3), numbers[3] === undefined ? 1 : numbers[3]];
        };
        const foreground = color(getComputedStyle(element).color);
        let node = element;
        let background = [255, 255, 255, 1];
        while (node) {
            const candidate = color(getComputedStyle(node).backgroundColor);
            if (candidate[3] >= 0.99) { background = candidate; break; }
            node = node.parentElement;
        }
        return {foreground, background};
    }''')
    def luminance(rgb):
        channels = [item / 255 for item in rgb[:3]]
        channels = [item / 12.92 if item <= .04045 else ((item + .055) / 1.055) ** 2.4 for item in channels]
        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
    foreground = [values['foreground'][i] * values['foreground'][3] + values['background'][i] * (1 - values['foreground'][3]) for i in range(3)]
    first, second = sorted((luminance(foreground), luminance(values['background'])))
    ratio = (second + .05) / (first + .05)
    assert ratio >= 4.5, f'{theme} {selector} contrast={ratio:.2f} ({values})'
    return {'theme': theme, 'selector': selector, 'ratio': round(ratio, 2), **values}


def _request(page, method, path, body=None):
    response = getattr(page.request, method)(path, **({'data': body} if body is not None else {}))
    assert response.ok, f'{method.upper()} {path}: {response.status} {response.text()[:500]}'
    return response.json()


def _login(page, base, password):
    page.goto(base)
    page.get_by_role('button', name='Рабочее пространство Вход для сотрудников').click()
    page.get_by_label('Логин', exact=True).fill('browser-v2')
    page.get_by_label('Пароль', exact=True).fill(password)
    page.get_by_role('button', name='Войти в пространство').click()
    expect(page).to_have_url(base + '/admin/inbox')
    expect(page.get_by_role('heading', name='Сервисная почта', exact=True)).to_be_visible()


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    assert (ROOT / 'frontend/dist/index.html').is_file(), 'Build frontend first'
    checks, browser_errors, request_failures, console_errors, contrast_checks = [], [], [], [], []
    with tempfile.TemporaryDirectory(prefix='posteks-v2-browser-') as temporary:
        runtime = os.environ.copy()
        for name in list(runtime):
            if name.startswith(('OPENAI_', 'POSTEKS_OPENAI_', 'MAIL_', 'IMAP_', 'SMTP_')) or name in {'AI_TOKEN', 'BOT_TOKEN'}:
                runtime[name] = ''
        # Empty values also prevent load_dotenv from rehydrating real credentials.
        runtime.update({name: '' for name in ('OPENAI_API_KEY', 'POSTEKS_OPENAI_API_KEY', 'AI_TOKEN',
            'MAIL_USERNAME', 'MAIL_ADDRESS', 'MAIL_APP_PASSWORD', 'MAIL_OAUTH_TOKEN', 'MAIL_IMAP_HOST',
            'IMAP_USER', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_HOST', 'SMTP_PASSWORD', 'BOT_TOKEN')})
        runtime.update(STORAGE_DIR=temporary, SERVICE_STORAGE_ROOT=temporary,
            DATABASE_URL='sqlite:///' + str(Path(temporary) / 'posteks_ai.db'), DB_URL='',
            APP_MODE='TEST', POSTEKS_REFERENCE_TEST_MODE='true', COOKIE_SECURE='false', PUBLIC_BASE_URL='', ALLOWED_ORIGINS='',
            MAIL_POLL_ENABLED='false', CORPORATE_EMAIL_DOMAIN='posteks.test',
            CORPORATE_EMAIL_DOMAINS='posteks.test', MAIL_USERNAME='service@posteks.test', MAIL_ADDRESS='service@posteks.test',
            OPENAI_MODEL='gpt-4.1-mini', OPENAI_FAST_MODEL='gpt-4.1',
            OPENAI_VISION_MODEL='gpt-4.1-mini', TESSERACT_CMD='/nonexistent/posteks-browser-test-tesseract', TESSDATA_PREFIX=str(ROOT / 'storage/tessdata'))
        password = secrets.token_urlsafe(24)
        prepared = subprocess.run([sys.executable, '-m', 'scripts.manage', 'create-admin',
            '--username', 'browser-v2', '--password-stdin'], cwd=ROOT, env=runtime,
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
                    assert process.poll() is None, 'Isolated server exited during startup'
                    time.sleep(.2)
                else:
                    raise RuntimeError('Isolated server failed readiness')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True,
                        executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),
                        args=['--no-sandbox', '--disable-dev-shm-usage'])
                    guest_context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                        locale='ru-RU', timezone_id='Asia/Yekaterinburg')
                    guest_context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
                    client = guest_context.new_page()
                    client.on('pageerror', lambda error: browser_errors.append(str(error)))
                    client.on('requestfailed', lambda request: request_failures.append(request.url))
                    client.on('dialog', lambda dialog: dialog.accept())
                    client.set_default_timeout(15000)
                    client.goto(base)
                    expect(client.get_by_role('heading', name='Сервис рядом. Решение — ближе.')).to_be_visible()
                    assert client.request.get(base + '/api/diagnostics').status == 401
                    assert client.request.get(base + '/api/documents').status == 401
                    assert client.locator('.sidebar-bottom').inner_text().strip() == 'ПОСТЭКС'
                    for selector in ('.starter-card strong', '.starter-card > span', '.new-chat-button', '.chat-header-actions .button'):
                        contrast_checks.append(_contrast(client, selector, 'light'))
                    client.screenshot(path=str(OUTPUT / 'chat-light.png'), full_page=True, animations='disabled')
                    client.get_by_role('button', name='Включить тёмную тему', exact=True).click()
                    expect(client.locator('html')).to_have_attribute('data-theme', 'dark')
                    client.wait_for_timeout(300)  # Settle the intentional .2s color/background theme transitions.
                    expect(client.locator('.brand img')).to_be_visible()
                    for selector in ('.starter-card strong', '.starter-card > span', '.new-chat-button', '.chat-header-actions .button'):
                        contrast_checks.append(_contrast(client, selector, 'dark'))
                    client.screenshot(path=str(OUTPUT / 'chat-dark.png'), full_page=True, animations='disabled')
                    client.get_by_role('button', name='Включить светлую тему', exact=True).click()
                    checks.append('Guest: live backend, protected staff APIs401, readable Cyrillic, both themes, footer without fixed year')

                    marking = 'ТЕСТОВАЯ МАРКИРОВКА\nМодель PE38-12. Сведения сообщены клиентом.'
                    client.locator('.composer input[type=file]').set_input_files({'name': 'test-marking.txt',
                        'mimeType': 'text/plain', 'buffer': marking.encode()})
                    expect(client.locator('.pending-attachments')).to_contain_text('test-marking.txt')
                    client.get_by_label('Ваш вопрос', exact=True).fill('Не понимаю, как определить нужный параметр. Модель PE38-12.')
                    with client.expect_response(lambda response: response.url.endswith('/api/chat') and response.request.method == 'POST') as sent:
                        client.get_by_role('button', name='Отправить вопрос', exact=True).click()
                    created = sent.value.json()
                    assert sent.value.status == 200, created
                    conversation_id = created['conversation_id']
                    expect(client.locator('.assistant-message')).to_have_count(1)
                    expect(client.locator('.chat-file')).to_contain_text('test-marking.txt')
                    assert not client.locator('.source-card').count(), 'No source may be invented in an empty knowledge base'
                    state = _request(client, 'get', base + '/api/conversations/' + conversation_id)
                    attachment_url = state['messages'][0]['attachments'][0]['url']
                    assert client.request.get(base + attachment_url).status == 200
                    other = browser.new_context()
                    assert other.request.get(base + attachment_url).status == 404
                    assert other.request.get(base + '/api/conversations/' + conversation_id).status == 404
                    other.close()
                    client.get_by_role('button', name='Позвать специалиста', exact=True).click()
                    client.get_by_label('Что передать специалисту?').fill('ТЕСТ: нужна дистанционная проверка маркировки, без выезда.')
                    client.get_by_role('button', name='Передать запрос', exact=True).click()
                    expect(client.locator('.client-handoff-status')).to_be_visible()
                    expect(client.locator('.client-handoff-status')).to_contain_text('Ответ специалиста появится')
                    checks.append('Client TXT attachment persisted privately; other guest denied; real handoff retains owned dialogue')

                    staff_context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                        locale='ru-RU', timezone_id='Asia/Yekaterinburg')
                    staff = staff_context.new_page()
                    staff.set_default_timeout(15000)
                    staff.on('pageerror', lambda error: browser_errors.append(str(error)))
                    staff.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
                    staff.on('dialog', lambda dialog: dialog.accept())
                    _login(staff, base, password)
                    expect(staff.get_by_text('Реквизиты IMAP не настроены', exact=True)).to_be_visible()
                    expect(staff.get_by_role('button', name='Синхронизировать сейчас', exact=True)).to_be_disabled()
                    expect(staff.get_by_role('button', name='Проверить подключение', exact=True)).to_be_disabled()
                    staff.get_by_role('button', name='Показать журнал и контрольные точки').click()
                    expect(staff.get_by_text('Синхронизация ещё не запускалась.', exact=False)).to_be_visible()
                    staff.screenshot(path=str(OUTPUT / 'mail-unconfigured.png'), full_page=True, animations='disabled')
                    checks.append('Mail shows actual unconfigured state and disabled connection/sync actions, not a false connected empty inbox')

                    staff.goto(base + '/admin/escalations')
                    expect(staff.locator('.escalation-card')).to_have_count(1)
                    staff.locator('.escalation-card').click()
                    expect(staff.get_by_role('heading', name='История диалога', exact=True)).to_be_visible()
                    staff.get_by_role('button', name='Взять в работу', exact=True).click()
                    expect(staff.get_by_text('Изменения сохранены.', exact=True)).to_be_visible()
                    reply = 'ТЕСТОВЫЙ ОТВЕТ СПЕЦИАЛИСТА\n\nПроверим маркировку дистанционно.\nВыезд не согласован.'
                    staff.get_by_label('Сообщение клиенту', exact=True).fill(reply)
                    staff.get_by_role('button', name='Ответить в диалоге', exact=True).click()
                    expect(staff.get_by_text('Ответ добавлен в тот же клиентский диалог. Email не отправлялся.', exact=True)).to_be_visible()
                    staff.screenshot(path=str(OUTPUT / 'escalation.png'), full_page=True, animations='disabled')
                    client.get_by_role('button', name='Обновить ответы специалиста', exact=True).click()
                    expect(client.locator('.specialist-message')).to_contain_text('ТЕСТОВЫЙ ОТВЕТ СПЕЦИАЛИСТА')
                    client.locator('.specialist-message').get_by_role('button', name='Скопировать', exact=True).click()
                    expect(client.locator('.specialist-message').get_by_role('button', name='Скопировано', exact=True)).to_be_visible()
                    assert client.evaluate('navigator.clipboard.readText()') == reply
                    assert len(_request(staff, 'get', base + '/api/cases')) == 1
                    assert len(_request(staff, 'get', base + '/api/escalations')) == 1
                    client.screenshot(path=str(OUTPUT / 'chat-specialist.png'), full_page=True, animations='disabled')
                    checks.append('Staff takes request and replies into same guest chat; multiline clipboard exact; one case/escalation')

                    staff.goto(base + '/admin/knowledge')
                    staff.get_by_role('button', name='Импорт истории переписки', exact=True).click()
                    transcript = {'messages': [{'sender': 'Тестовый инженер', 'date': '2026-10-22',
                        'conversation_id': 'TEST-ONLY', 'text': 'ТЕСТОВЫЙ НЕПРОВЕРЕННЫЙ ОПЫТ. PE38-12: уточнили маркировку; вывод должен проверить сотрудник.'}]}
                    staff.locator('.history-import input[type=file]').set_input_files({'name': 'test-history.json',
                        'mimeType': 'application/json', 'buffer': json.dumps(transcript, ensure_ascii=False).encode()})
                    expect(staff.locator('.history-preview')).to_contain_text('ТЕСТОВЫЙ НЕПРОВЕРЕННЫЙ ОПЫТ')
                    staff.get_by_label('Я проверил файл и разрешаю сохранить его как внутренний непроверенный источник.').check()
                    staff.get_by_role('button', name='Подтвердить внутренний импорт', exact=True).click()
                    expect(staff.get_by_text('История сохранена как внутренний непроверенный источник', exact=False)).to_be_visible()
                    imports = _request(staff, 'get', base + '/api/history/imports')
                    assert len(imports) == 1 and imports[0]['visibility'] == 'internal'
                    assert client.request.get(base + imports[0]['raw_url']).status == 401
                    checks.append('Transcript UI preview, explicit private import, persisted provenance and guest download denied')

                    staff.goto(base + '/admin/playground')
                    expect(staff.get_by_role('heading', name='AI Playground', exact=True)).to_be_visible()
                    cases_before = len(_request(staff, 'get', base + '/api/cases'))
                    staff.get_by_label('Искать в документации').uncheck()
                    staff.get_by_label('Тестовый вопрос по базе знаний', exact=True).fill('Мне нужен специалист. Модель PE38-12.')
                    with staff.expect_response(lambda response: response.url.endswith('/api/playground') and response.request.method == 'POST') as first_test:
                        staff.get_by_role('button', name='Задать тестовый вопрос', exact=True).click()
                    first = first_test.value.json()
                    assert first_test.value.status == 200, first
                    try:
                        expect(staff.locator('.test-message.assistant')).to_have_count(1)
                    except Exception:
                        (OUTPUT / 'playground-response.json').write_text(json.dumps({'response': first,
                            'browser_errors': browser_errors, 'console_errors': console_errors, 'page_text': staff.locator('body').inner_text()},
                            ensure_ascii=False, indent=2), encoding='utf-8')
                        staff.screenshot(path=str(OUTPUT / 'failure.png'), full_page=True, animations='disabled')
                        raise
                    expect(staff.locator('.trace-content')).to_contain_text('Классификация')
                    staff.get_by_label('Тестовый вопрос по базе знаний', exact=True).fill('Адрес: Тестовая улица 1. Удобно завтра в 11:00.')
                    with staff.expect_response(lambda response: response.url.endswith('/api/playground') and response.request.method == 'POST') as second_test:
                        staff.get_by_role('button', name='Задать тестовый вопрос', exact=True).click()
                    second = second_test.value.json()
                    assert second_test.value.status == 200 and second['conversation_id'] == first['conversation_id']
                    assert second['state']['turn_count'] == 2
                    assert len(_request(staff, 'get', base + '/api/cases')) == cases_before, 'Playground caused business effects'
                    staff.get_by_label('Замечание к ответу').fill('ТЕСТ: состояние и запрет реальных действий сохранены.')
                    staff.get_by_role('button', name='Верно', exact=True).click()
                    expect(staff.get_by_text('Оценка ответа сохранена.', exact=True)).to_be_visible()
                    staff.get_by_role('button', name='Тестовые сценарии', exact=True).click()
                    staff.get_by_label('Название сценария', exact=True).fill('ТЕСТ: сохранение цели консультации')
                    staff.get_by_label('Что должен проверить тест', exact=True).fill('Данные двух сообщений сохраняются; обращение не создаётся.')
                    staff.get_by_role('button', name='Сохранить 2 шагов', exact=True).click()
                    expect(staff.get_by_text('Тестовый сценарий сохранён.', exact=False)).to_be_visible()
                    saved = _request(staff, 'get', base + '/api/playground/scenarios')
                    assert len(saved) == 1 and len(saved[0]['steps']) == 2
                    staff.screenshot(path=str(OUTPUT / 'playground.png'), full_page=True, animations='disabled')
                    checks.append('Playground real persistent two-turn memory, no business actions, trace, evaluation and scenario persisted')
                    staff.locator('.scenario-entry > button').first.click()
                    expect(staff.get_by_label('Тестовый вопрос по базе знаний', exact=True)).to_have_value(saved[0]['steps'][0]['message'])
                    with staff.expect_response(lambda response: response.url.endswith('/api/playground') and response.request.method == 'POST') as replay_first:
                        staff.get_by_role('button', name='Задать тестовый вопрос', exact=True).click()
                    replay = replay_first.value.json()
                    assert replay_first.value.status == 200 and replay['conversation_id'] != first['conversation_id']
                    staff.get_by_role('button', name='Следующий шаг сценария', exact=True).click()
                    expect(staff.get_by_label('Тестовый вопрос по базе знаний', exact=True)).to_have_value(saved[0]['steps'][1]['message'])
                    with staff.expect_response(lambda response: response.url.endswith('/api/playground') and response.request.method == 'POST') as replay_second:
                        staff.get_by_role('button', name='Задать тестовый вопрос', exact=True).click()
                    replayed = replay_second.value.json()
                    assert replay_second.value.status == 200 and replayed['conversation_id'] == replay['conversation_id']
                    assert replayed['state']['turn_count'] == 2
                    expect(staff.get_by_text('Все шаги выполнены.', exact=False)).to_be_visible()
                    staff.get_by_role('button', name='Удалить сценарий ТЕСТ: сохранение цели консультации', exact=True).click()
                    expect(staff.locator('.scenario-entry')).to_have_count(0)
                    assert _request(staff, 'get', base + '/api/playground/scenarios') == []
                    assert len(_request(staff, 'get', base + '/api/cases')) == cases_before
                    checks.append('Saved two-step scenario replay starts isolated conversation, retains state, advances steps and deletes real saved record')
                    staff.get_by_label('Тестовый вопрос по базе знаний', exact=True).fill('ТЕСТ: сравнить ответы моделей.')
                    staff.get_by_role('button', name='Сравнить', exact=True).click()
                    expect(staff.get_by_role('alert')).to_contain_text('задайте серверный ключ OpenAI')
                    assert not staff.locator('.playground-comparison article').count(), 'Comparison may not invent model answers without a key'
                    checks.append('Model comparison without credentials displays server error and creates no simulated answers')

                    staff.goto(base + '/admin/inbox')
                    staff.locator('input[type=file]').set_input_files(str(ROOT / 'tests/fixtures/service-request.eml'))
                    expect(staff.locator('.ws-company-row')).to_have_count(1)
                    staff.get_by_role('button', name='По письмам', exact=True).click()
                    expect(staff.locator('.ws-email-row')).to_have_count(1)
                    staff.locator('.ws-email-row').click()
                    expect(staff.locator('.ws-mail-text')).to_contain_text('ТЕСТОВОЕ ПИСЬМО')
                    staff.get_by_role('button', name='Кандидат вручную', exact=True).click()
                    candidate = staff.locator('.mail-candidate-form')
                    candidate.get_by_label('Модель оборудования', exact=True).fill('PE38-12')
                    candidate.get_by_label('Симптомы', exact=True).fill('ТЕСТ: клиент уточняет маркировку.')
                    candidate.get_by_label('Действия и результат', exact=True).fill('ТЕСТ: запросили фотографию шильдика. Техническое решение ещё не подтверждено.')
                    candidate.get_by_label('Ограничения', exact=True).fill('Не техническое правило; требуется проверка инженером.')
                    candidate.get_by_role('button', name='Сохранить кандидата', exact=True).click()
                    expect(staff.get_by_text('сохранён как внутренний непроверенный опыт.', exact=False)).to_be_visible()
                    solutions = _request(staff, 'get', base + '/api/solutions')
                    assert len(solutions) == 1 and solutions[0]['status'] == 'CANDIDATE' and solutions[0]['visibility'] == 'internal'
                    assert _request(staff, 'get', base + '/api/mail/status')['sending_enabled'] is False
                    assert len(_request(staff, 'get', base + '/api/cases')) == cases_before, 'Imported mail became a service case without operator action'
                    assert len(_request(staff, 'get', base + '/api/cases?kind=correspondence')) == 1
                    checks.append('Real TEST EML grouped by customer; thread to editable unverified solution candidate; ordinary mail is not a formal case; outgoing remains disabled')

                    # Classification and chronology must survive out-of-order imports.
                    def add_mail(sender, recipient, subject, message_id, date, text, reference=None, attachment=False):
                        mail = EmailMessage()
                        mail['From'], mail['To'], mail['Subject'] = sender, recipient, subject
                        mail['Message-ID'], mail['Date'] = f'<{message_id}@posteks.test>', date
                        if reference:
                            mail['In-Reply-To'] = f'<{reference}@posteks.test>'
                            mail['References'] = f'<{reference}@posteks.test>'
                        mail.set_content(text)
                        if attachment:
                            mail.add_attachment(b'TEST ATTACHMENT CONTENT', maintype='text', subtype='plain', filename='test-part.txt')
                        response = staff.request.post(base + '/api/mail/import', multipart={'files': {
                            'name': message_id + '.eml', 'mimeType': 'message/rfc822', 'buffer': mail.as_bytes()}})
                        assert response.ok, response.text()
                        assert response.json()['imported'] == 1, response.json()
                    add_mail('Buyer <buyer@elsan.test>', 'service@posteks.test', 'ТЕСТ ЭЛСАН: ремень', 'alpha-1',
                             'Tue, 20 Oct 2026 10:00:00 +0000', 'ТЕСТ: нужна маркировка.\nООО «Тест Элсан»', attachment=True)
                    add_mail('Engineer <engineer@posteks.test>', 'service@posteks.test', 'Re: ТЕСТ ЭЛСАН: ремень', 'staff-1',
                             'Sat, 24 Oct 2026 10:00:00 +0000', 'ТЕСТ ВНУТРЕННИЙ: сначала проверить модель.', reference='alpha-1')
                    add_mail('Service <service@posteks.test>', 'buyer@elsan.test', 'Re: ТЕСТ ЭЛСАН: ремень', 'sent-1',
                             'Thu, 22 Oct 2026 10:00:00 +0000', 'ТЕСТ ОТПРАВЛЕННЫЙ: пожалуйста, пришлите шильдик.', reference='alpha-1')
                    add_mail('Buyer <buyer@elsan.test>', 'service@posteks.test', 'Re: ТЕСТ ЭЛСАН: ремень', 'alpha-2',
                             'Fri, 23 Oct 2026 10:00:00 +0000', 'ТЕСТ: данные машины уточняю.', reference='alpha-1')
                    add_mail('Other <other@gmail.com>', 'service@posteks.test', 'ТЕСТ: другой клиент', 'beta-1',
                             'Sun, 25 Oct 2026 10:00:00 +0000', 'ТЕСТ: отдельный запрос.')
                    add_mail('Third <third@gmail.com>', 'service@posteks.test', 'ТЕСТ: третий клиент', 'gamma-1',
                             'Wed, 21 Oct 2026 10:00:00 +0000', 'ТЕСТ: ещё один отдельный запрос.')
                    assert len(_request(staff, 'get', base + '/api/cases')) == cases_before
                    staff.goto(base + '/admin/inbox')
                    expect(staff.locator('.ws-company-row')).to_have_count(4)
                    staff.set_viewport_size({'width': 1680, 'height': 1400})
                    staff.evaluate("document.querySelector('.workspace-content').scrollTo(0,0);window.scrollTo(0,0)")
                    staff.screenshot(path=str(OUTPUT / 'inbox-company-overview.png'), full_page=False, animations='disabled')
                    staff.locator('.ws-company-row').filter(has_text='buyer@elsan.test').click()
                    expect(staff.locator('.ws-email-row')).to_have_count(1)
                    staff.locator('.ws-email-row').click()
                    expect(staff.locator('.mail-thread-entry')).to_have_count(4)
                    expect(staff.locator('.mail-thread')).to_contain_text('ТЕСТ ВНУТРЕННИЙ')
                    expect(staff.locator('.mail-thread')).to_contain_text('ТЕСТ ОТПРАВЛЕННЫЙ')
                    expect(staff.locator('.mail-thread')).to_contain_text('данные машины уточняю')
                    expect(staff.get_by_label('Кому', exact=True)).to_have_value('buyer@elsan.test')
                    download = staff.locator('.inbox-attachments a')
                    expect(download).to_have_count(1)
                    assert staff.request.get(base + download.get_attribute('href')).body() == b'TEST ATTACHMENT CONTENT'
                    with staff.expect_response(lambda response: '/draft' in response.url and response.request.method == 'POST') as unavailable_draft:
                        staff.get_by_role('button', name='Подготовить черновик ИИ', exact=True).click()
                    assert unavailable_draft.value.status == 503
                    expect(staff.get_by_role('alert').first).to_be_visible()
                    assert _request(staff, 'get', base + '/api/approvals') == [], 'Unavailable provider fabricated a draft'
                    staff.get_by_role('button', name='Создать сервисное обращение', exact=True).click()
                    expect(staff.get_by_role('button', name=re.compile('^Сервисное обращение #'))).to_be_visible()
                    assert len(_request(staff, 'get', base + '/api/cases')) == cases_before + 1
                    staff.locator('.inbox-category-tabs').evaluate("element=>element.scrollIntoView({block:'start'})")
                    staff.screenshot(path=str(OUTPUT / 'inbox-company-thread.png'), full_page=False, animations='disabled')
                    staff.set_viewport_size({'width': 1440, 'height': 1000})
                    staff.get_by_role('button', name='Ответы сотрудников', exact=True).click()
                    expect(staff.locator('.ws-company-row')).to_have_count(1)
                    staff.get_by_role('button', name='По письмам', exact=True).click()
                    expect(staff.locator('.ws-email-row')).to_have_count(1)
                    expect(staff.locator('.ws-email-row')).to_contain_text('Внутри компании')
                    staff.get_by_role('button', name='Отправленные', exact=True).click()
                    expect(staff.locator('.ws-email-row')).to_have_count(1)
                    expect(staff.locator('.ws-email-row')).to_contain_text('Отправлено')
                    staff.get_by_role('button', name='Клиенты', exact=True).click()
                    expect(staff.locator('.ws-email-row')).to_have_count(5)
                    expect(staff.locator('.ws-email-row').first).to_contain_text('other@gmail.com')
                    staff.get_by_label('Сортировка писем', exact=True).select_option('oldest')
                    expect(staff.locator('.ws-email-row').first).to_contain_text('customer@example.com')
                    staff.get_by_label('Письма с даты', exact=True).fill('2026-10-23')
                    expect(staff.locator('.ws-email-row')).to_have_count(2)
                    customers = _request(staff, 'get', base + '/api/mail/customers')
                    alpha = next(item for item in customers if item['email'] == 'buyer@elsan.test')
                    assert alpha['new_count'] == 0, 'Opening thread did not clear its local new indicators'
                    assert len([item for item in customers if item['email'].endswith('@gmail.com')]) == 2, 'Public mail provider merged unrelated customers'
                    checks.append('Out-of-order EML: company thread includes client/staff/sent; local read clears indicator; attachment bytes; AI503 no fake draft; explicit promotion; category/date/sort and separate public-email customers')
                    staff.goto(base + '/admin/knowledge')
                    # A failed scan page must not discard a readable PDF page.
                    import pymupdf as fitz
                    pdf = fitz.open()
                    first_page = pdf.new_page()
                    first_page.insert_text((50, 50), 'TEST MANUAL PE38-12. Keep the readable text layer even when OCR is unavailable. ' * 3)
                    scan_page = pdf.new_page()
                    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
                    pixmap.clear_with(255)
                    scan_page.insert_image(fitz.Rect(50, 50, 300, 300), stream=pixmap.tobytes('png'))
                    pdf_bytes = pdf.tobytes()
                    pdf.close()
                    staff.locator('.knowledge-main>input[type=file]').set_input_files({'name': 'test-partial-manual.pdf',
                        'mimeType': 'application/pdf', 'buffer': pdf_bytes})
                    partial_row = staff.locator('.document-row').filter(has_text='test-partial-manual')
                    expect(partial_row).to_contain_text('В поиске · есть замечания', timeout=30000)
                    expect(partial_row).to_contain_text('Текст найден на 1 из 2 страниц.')
                    expect(partial_row.get_by_role('button', name='Повторить обработку', exact=True)).to_be_enabled()
                    partial_row.locator('.doc-select').click()
                    expect(staff.locator('.source-drawer')).to_contain_text('Keep the readable text layer')
                    original = staff.locator('.source-drawer').get_by_role('link', name='Открыть оригинал')
                    assert staff.request.get(base + original.get_attribute('href').split('#')[0]).body() == pdf_bytes
                    staff.get_by_role('button', name='Закрыть источник', exact=True).click()
                    staff.locator('.knowledge-main').evaluate('element=>element.scrollTo(0,0)')
                    staff.screenshot(path=str(OUTPUT / 'knowledge-partial-ocr.png'), full_page=False, animations='disabled')
                    checks.append('Actual PDF with text plus scan: missing Tesseract preserves searchable first page, amber partial warning/count, retry action and original PDF bytes')
                    expect(staff.get_by_role('heading', name='Поиск по смыслу', exact=True)).to_be_visible()
                    expect(staff.get_by_role('button', name='Подготовить следующую порцию · до 50', exact=True)).to_be_disabled()
                    expect(staff.locator('.semantic-search-panel')).to_contain_text('Работает текстовый поиск')
                    checks.append('Semantic index UI exposes real unconfigured status, lexical fallback, disabled paid build without credentials')

                    staff.goto(base + '/admin/settings')
                    expect(staff.get_by_role('heading', name='Диагностика хранилища', exact=True)).to_be_visible()
                    staff.get_by_role('button', name='Показать технические сведения', exact=True).click()
                    expect(staff.locator('.diagnostics-values')).to_contain_text(temporary)
                    expect(staff.locator('.diagnostics-values')).to_contain_text('posteks_ai.db')
                    diagnostics = _request(staff, 'get', base + '/api/diagnostics')
                    assert diagnostics['counts']['documents'] == 2 and diagnostics['counts']['internal'] == 2
                    staff.get_by_role('button', name='Тёмная', exact=True).click()
                    staff.screenshot(path=str(OUTPUT / 'settings-dark.png'), full_page=True, animations='disabled')
                    checks.append('Staff diagnostics shows actual temporary DB and private document counts, not a simulated success')

                    for theme in ('light', 'dark'):
                        for scale in (1, 1.25, 1.5):
                            scaled = browser.new_context(viewport={'width': round(1440 / scale), 'height': round(1000 / scale)},
                                device_scale_factor=scale, locale='ru-RU', storage_state=staff_context.storage_state())
                            scaled.add_init_script(f"localStorage.setItem('posteks-theme','{theme}')")
                            scaled_page = scaled.new_page()
                            scaled_page.on('pageerror', lambda error: browser_errors.append(str(error)))
                            scaled_page.goto(base + '/admin/escalations')
                            expect(scaled_page.get_by_role('heading', name=re.compile('^Требуется специалист'))).to_be_visible()
                            _no_overflow(scaled_page, f'{theme} scale{scale}')
                            font = scaled_page.locator('.sidebar-bottom').evaluate('(node)=>parseFloat(getComputedStyle(node).fontSize)')
                            assert font >= 13, f'Unreadable footer size {font}'
                            scaled_page.screenshot(path=str(OUTPUT / f'scale-{theme}-{round(scale*100)}.png'), full_page=True, animations='disabled')
                            scaled.close()
                    checks.append('Light/dark with 100/125/150% display scaling emulation: no overflow, Cyrillic labels >=13px')

                    mobile_context = browser.new_context(viewport={'width': 390, 'height': 844},
                        is_mobile=True, has_touch=True, locale='ru-RU', storage_state=staff_context.storage_state())
                    mobile_context.add_init_script("localStorage.setItem('posteks-theme','dark')")
                    mobile = mobile_context.new_page()
                    mobile.on('pageerror', lambda error: browser_errors.append(str(error)))
                    for route in ('inbox', 'escalations', 'knowledge', 'playground', 'settings'):
                        mobile.goto(base + '/admin/' + route)
                        expect(mobile.locator('.workspace-topbar')).to_be_visible()
                        _no_overflow(mobile, 'mobile ' + route)
                    mobile.goto(base + '/admin/escalations')
                    mobile.screenshot(path=str(OUTPUT / 'mobile-escalations-dark.png'), full_page=True, animations='disabled')
                    mobile.goto(base)
                    mobile.get_by_role('button', name='Открыть меню', exact=True).click()
                    expect(mobile.get_by_role('button', name=re.compile('^Новый диалог'))).to_be_visible()
                    mobile.get_by_role('button', name=re.compile('^Новый диалог')).click()
                    _no_overflow(mobile, 'mobile chat')
                    mobile.screenshot(path=str(OUTPUT / 'mobile-chat-dark.png'), full_page=True, animations='disabled')
                    checks.append('390px mobile: inbox, handoff queue, knowledge, Playground, settings, chat menu without horizontal overflow')
                    assert not browser_errors, 'Runtime JS errors: ' + '; '.join(browser_errors)
                    caught_errors = [entry for entry in console_errors if not entry.startswith('Failed to load resource:')]
                    assert not caught_errors, 'Caught JS errors: ' + '; '.join(caught_errors)
                    browser.close()
            except Exception as exc:
                (OUTPUT / 'report.json').write_text(json.dumps({'status': 'failed', 'checks': checks,
                    'browser_errors': browser_errors, 'console_errors': console_errors, 'request_failures': request_failures,
                    'error': str(exc)}, ensure_ascii=False, indent=2), encoding='utf-8')
                try:
                    if 'staff' in locals() and not staff.is_closed():
                        staff.screenshot(path=str(OUTPUT / 'failure.png'), full_page=True, animations='disabled')
                except Exception:
                    pass
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    report = {'status': 'passed', 'checks': checks, 'contrast_checks': contrast_checks, 'browser_errors': browser_errors,
        'caught_javascript_errors': caught_errors,
        'request_failures': request_failures,
        'note': 'Real isolated TEST backend with synthetic data. No company sources, original DB, real IMAP/OpenAI, or Windows execution. Scaling is browser device/viewport emulation.'}
    (OUTPUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
