"""One-click local diagnostics. Working data is read, never migrated or imported.

Live mode uses the real configured provider and read-only IMAP sampling. File
uploads and conversations run through actual routes in a disposable database.
Reports distinguish a returned answer, a deterministic check and a skipped test;
they are not an engineering certification of generated recommendations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version, PackageNotFoundError
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
import secrets
from collections import Counter
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CUSTOMER_SYMPTOM = ('Периодически привод останавливается после захвата брикетов. Как будто чего-то ждёт. '
    'После ручного перемещения и сброса запуска машина снова работает. Какие сведения нужны для безопасной диагностики?')


def result(identifier, status, title, details, evidence=None, resolution=''):
    return {'id': identifier, 'status': status, 'title': title, 'details': details,
            'evidence': evidence or {}, 'resolution': resolution}


def check_citation_validation():
    from types import SimpleNamespace
    from server.ai import ProviderResponseFailure, _validate_citations
    from server.orchestrator import TechnicalClaim

    source = 'Контрольная\nстрока из теста.'
    claim = TechnicalClaim(source_index=1, quote='Контрольная строка из теста.', text='Контрольная строка')
    plan = SimpleNamespace(technical_claims=[claim])
    sources = [{'text': source}]
    rejection = None
    restored = False
    try:
        _validate_citations(plan, sources)
        restored = claim.quote == source and claim.text == 'Контрольная\nстрока'
        claim.text = 'Выдуманное утверждение'
        _validate_citations(plan, sources)
    except ProviderResponseFailure as exc:
        rejection = exc.details.get('citation_error')
    passed = restored and rejection == 'claim_not_in_quote'
    return result('citation.validation', 'pass' if passed else 'fail', 'Проверка цитат',
        'Искусственная цитата проверяет восстановление переносов строк и отклонение неподтверждённого утверждения. Запроса к модели нет.',
        {'citation_error': rejection}, '' if passed else 'Передайте support_report.json для разбора проверки цитат.')


@contextmanager
def working_database():
    """Use a separate read-only connection, including when start.bat is running."""
    from server.config import settings
    from sqlalchemy.engine import make_url
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'sqlite' or not url.database or not Path(url.database).is_file():
        yield None
        return
    path = Path(url.database).resolve()
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(
        path.as_uri() + '?mode=ro', uri=True, check_same_thread=False, timeout=5))
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def local_checks(session):
    from sqlalchemy import select, func, text
    from server.config import settings
    from server.models import Document, AppSetting
    from server.workflows import VerifiedSolution
    from server.source_access import can_use_for_answer
    from server.ocr import diagnostic
    from server.media import VIDEO_TOOL_SETUP, video_tool_status
    checks, material = [], None
    index = ROOT / 'frontend/dist/index.html'
    assets = re.findall(r'(?:src|href)="(/assets/[^"?#]+)', index.read_text(encoding='utf-8')) if index.is_file() else []
    assets_ok = bool(assets) and all((ROOT/'frontend/dist'/name.lstrip('/')).is_file() for name in assets)
    checks.append(result('files.frontend', 'pass' if assets_ok else 'fail', 'Файлы интерфейса',
        'Проверены HTML и указанные в нём собранные JS/CSS.', {'checked': len(assets)},
        '' if assets_ok else 'Обновите программные файлы целиком и повторите install.bat.'))
    ocr = diagnostic()
    checks.append(result('files.ocr', 'pass' if ocr['available'] else 'warn', 'Распознавание сканов',
        'Проверены запуск Tesseract и языки rus/eng; текстовые PDF обрабатываются независимо от OCR.',
        {'code': ocr.get('code'), 'available': ocr['available']},
        '' if ocr['available'] else 'Укажите TESSERACT_CMD и выполните python -m scripts.install_ocr из виртуальной среды.'))
    video_status = video_tool_status()
    video_codes = [f'{tool}_{state}' for tool, state in video_status.items() if state != 'ready']
    video_ok = not video_codes
    checks.append(result('files.video_tools', 'pass' if video_ok else 'warn', 'Средства обработки видео',
        'Проверен локальный запуск FFmpeg и ffprobe без чтения видео.',
        {'available': video_ok, 'ffmpeg_available': video_status['ffmpeg'] == 'ready',
         'ffprobe_available': video_status['ffprobe'] == 'ready', 'code': next(iter(video_codes), None)},
        '' if video_ok else VIDEO_TOOL_SETUP))
    if session is None:
        checks.append(result('database.read', 'fail', 'Рабочая база', 'Существующая SQLite не найдена или выбран иной тип БД.',
            resolution='Проверьте DATABASE_URL и STORAGE_DIR. Тест не создаёт пустую базу вместо рабочей.'))
        return checks, material
    integrity = session.execute(text('PRAGMA quick_check(1)')).scalar()
    checks.append(result('database.read', 'pass' if integrity == 'ok' else 'fail', 'Чтение и целостность рабочей базы',
        'Открыта отдельная SQLite в режиме только чтения. Миграции и исправления не выполнялись.', {'complete': integrity == 'ok'}))
    docs = list(session.scalars(select(Document).where(Document.deleted.is_(False))))
    indexed = sum(d.status == 'indexed' for d in docs)
    errors = sum(d.status == 'error' for d in docs)
    checks.append(result('knowledge.index', 'fail' if not docs or errors else 'pass' if indexed == len(docs) else 'warn',
        'Документы и поисковый индекс', 'Проверены реальные записи; очередь, ошибки и готовый индекс различаются.',
        {'total': len(docs), 'indexed': indexed, 'errors': errors},
        'При ошибках проверьте оригинал и OCR, затем повторите обработку документа в базе знаний.'))
    allowed = sum(can_use_for_answer(d) for d in docs)
    checks.append(result('knowledge.client_access', 'pass' if allowed else 'warn', 'Знания для клиентских ответов',
        'Оригиналы могут оставаться закрытыми; разрешение на ответы задаётся отдельно.', {'total': allowed},
        '' if allowed else 'Администратор должен разрешить ответы по нужным техническим руководствам. Обычный разговор не требует документов.'))
    missing, checked, total_bytes = 0, 0, 0
    candidates = []
    for doc in sorted(docs, key=lambda d: (not can_use_for_answer(d), not bool(d.models), d.size_bytes, d.id)):
        path = (settings.documents_dir / doc.storage_name).resolve()
        if path.parent != settings.documents_dir.resolve() or not path.is_file() or path.is_symlink():
            missing += 1
            continue
        if checked < 100 and total_bytes + path.stat().st_size <= 256*1024*1024:
            checked += 1; total_bytes += path.stat().st_size
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc.sha256:
                missing += 1
                continue
        if doc.status == 'indexed' and doc.models and path.suffix.lower() in {'.pdf', '.txt', '.md'} and path.stat().st_size <= settings.max_file_bytes:
            candidates.append({'path': path, 'filename': doc.filename, 'model': doc.models[0], 'document_id': doc.id})
    material = candidates[0] if candidates else None
    checks.append(result('files.originals', 'fail' if missing else 'pass' if checked == len(docs) else 'warn',
        'Оригиналы и контрольные суммы', 'Наличие проверено по реальному хранилищу; SHA-256 ограничен 100 файлами и 256 МиБ за запуск.',
        {'total': len(docs), 'checked': checked, 'missing': missing},
        'При пропусках восстановите соответствующие оригиналы из резервной копии; рабочая БД тестом не менялась.'))
    verified = session.scalar(select(func.count()).select_from(VerifiedSolution).where(VerifiedSolution.status == 'VERIFIED')) or 0
    checks.append(result('knowledge.verified_cases', 'pass' if verified else 'warn', 'Подтверждённые решения',
        'Архивные письма и собственные ответы ИИ не считаются проверенными решениями.', {'total': verified},
        '' if verified else 'Сохраните подходящий случай как кандидат и проверьте его сотрудником перед использованием.'))
    return checks, material


def evaluate_turns(name, rows, contexts, *, expected_turns=None):
    """Evaluate observable transport and errors, not pretend to grade all semantics."""
    expected_turns = len(rows) if expected_turns is None else expected_turns
    succeeded = [r for r in rows if r.get('engine') == 'openai' and r.get('provider_response_id') and r.get('response')
                 and 200 <= r.get('http_status', 200) < 300]
    first_failed = next((r for r in rows if r not in succeeded), None)
    diagnostics = (first_failed or rows[-1] if rows else {}).get('provider_diagnostics') or {}
    evidence = {'expected_turns': expected_turns, 'successful_turns': len(succeeded), **diagnostics}
    if first_failed:
        error = first_failed.get('error_detail') or {}
        return [result('model.' + name, 'fail', 'Диалог: ' + name,
            error.get('message') or 'Приложение не получило пригодный ответ модели; HTTP 200 сам по себе не означает успех.',
            evidence, 'Смотрите этап, код и тип ошибки в отчёте для поддержки. Повторять все вопросы вручную не нужно.')]
    ids = {r.get('conversation_id') for r in rows}
    state_ok = len(rows) == expected_turns and len(ids) == 1 and None not in ids
    history_ok = len(contexts) == len(rows) and all(
        c.get('current_message') == r['input'] and c.get('history_items') == index*2
        for index, (c, r) in enumerate(zip(contexts, rows)))
    messages = [str(r.get('response') or '').strip() for r in rows]
    repeated = len([m for m in messages if len(m) >= 80]) != len(set(m for m in messages if len(m) >= 80))
    checks = [result('model.'+name, 'pass' if state_ok and history_ok else 'fail', 'Диалог: '+name,
        'Проверены настоящий вызов модели, новые сообщения, история и сохранение одного диалога.', evidence,
        '' if state_ok and history_ok else 'История или идентификатор диалога не совпали с ожидаемой последовательностью.')]
    if repeated:
        checks.append(result('model.'+name+'.repeat', 'warn', 'Дословный повтор ответа',
            'На разные реплики получен одинаковый длинный текст. Это повод проверить содержательный диалог, а не доказательство исправности.'))
    return checks


def application_turns(client, recorder, name, messages):
    cid, rows = None, []
    recorder.path = 'application'
    for number, message in enumerate(messages, 1):
        recorder.turn = name + ':' + str(number)
        response = client.post('/api/chat', json={'message': message, **({'conversation_id': cid} if cid else {})})
        body = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        cid = body.get('conversation_id', cid)
        rows.append({'input': message, 'response': body.get('message', {}).get('content'),
            'http_status': response.status_code, 'conversation_id': cid, 'engine': body.get('engine'),
            'provider_response_id': body.get('provider_response_id'), 'provider_diagnostics': body.get('provider_diagnostics'),
            'error_detail': body.get('error'), 'state': body.get('state'),
            'sources': body.get('message', {}).get('sources', [])})
        if body.get('engine') != 'openai':
            break  # One failed stage is enough; do not repeat paid failures.
    return rows


def negative_route_checks(client, factory):
    """Expected failures and inert archive commands, all in the disposable DB."""
    from server.models import User
    from server.auth import hash_password
    from server.workflows import ServiceCase, Approval, InternalTask
    from sqlalchemy import select, func
    password=secrets.token_urlsafe(24)
    with factory() as session:
        session.add(User(username='selfcheck-auditor', password_hash=hash_password(password), role='admin'))
        session.commit()
    client.post('/api/auth/login',json={'username':'selfcheck-auditor','password':password})
    invalid=client.post('/api/documents',files={'files':('invalid.pdf',b'THIS IS NOT A PDF','application/pdf')})
    blank=client.post('/api/chat',json={'message':''})
    checks=[result('negative.invalid_pdf','pass' if invalid.status_code==422 else 'fail','Повреждённый PDF отклонён',
        'Это намеренно неверный файл. Ожидаемая ошибка 422 означает успешную защитную проверку.',{'http_status':invalid.status_code}),
        result('negative.blank_message','pass' if blank.status_code==422 else 'fail','Пустое сообщение отклонено',
        'Это намеренно пустой запрос; он не должен вызывать модель.',{'http_status':blank.status_code})]
    with factory() as session:
        before=[session.scalar(select(func.count()).select_from(model)) for model in (ServiceCase,Approval,InternalTask)]
    raw=json.dumps({'messages':[{'message_id':'selfcheck-archive-1','conversation_id':'selfcheck-thread',
        'sender':'TEST','text':'TEST: пришлите счёт. Старый ответ: счёт отправлен.'}]},ensure_ascii=False).encode()
    first=client.post('/api/history/import',files={'file':('selfcheck-history.json',raw,'application/json')},
        data={'confirmed':'true','source':'manual'})
    second=client.post('/api/history/import',files={'file':('selfcheck-history.json',raw,'application/json')},
        data={'confirmed':'true','source':'manual'})
    with factory() as session:
        after=[session.scalar(select(func.count()).select_from(model)) for model in (ServiceCase,Approval,InternalTask)]
    duplicated=first.status_code==second.status_code==201 and first.json().get('id')==second.json().get('id')
    checks += [result('archive.no_actions','pass' if before==after and first.status_code==201 else 'fail',
        'Архивная просьба не запускает действия','Исторический текст не создаёт обращение, задачу, черновик или отправку.',
        {'total':sum(a-b for a,b in zip(after,before)),'http_status':first.status_code}),
        result('archive.deduplication','pass' if duplicated else 'fail','Повторный импорт без дубля',
        'Один и тот же архив повторно импортирован в одноразовую базу.',{'http_status':second.status_code})]
    client.post('/api/auth/logout');client.get('/api/auth/me')
    return checks


def exercise_application(models, material, *, live, max_calls=20):
    from server import ai
    from server.config import settings
    from scripts.dialogue_diagnostics import ProviderRecorder, isolated_application, install_material, BASIC_TURNS, run_direct
    from sqlalchemy import select
    from server.models import KnowledgeChunk
    recorder = ProviderRecorder(live=live, limit=max_calls, capture_content=False)
    checks, private = [], {}
    with tempfile.TemporaryDirectory(prefix='posteks-selfcheck-file-') as folder, ExitStack() as stack:
        if live:
            import openai
            stack.enter_context(patch.object(openai, 'OpenAI', recorder.wrap_constructor(openai.OpenAI)))
        with isolated_application(live=live, recorder=recorder, configured_models=models) as (client, factory):
            path = Path(folder)/(str(material.get('filename') or material['path'].name).replace('\\','/').rsplit('/',1)[-1]) if material else Path(folder)/'PE38-12-SELFTEST.txt'
            if material is not None:
                # Storage UUIDs are not original equipment filenames. Preserve
                # the real upload name when checking model recognition.
                shutil.copyfile(material['path'],path)
            if material is None:
                path.write_text('PE38-12\nSELFTEST: это искусственный материал проверки загрузки. Тестовая запись не является инструкцией ремонта.', encoding='utf-8')
            uploaded = install_material(client, factory, path)
            checks.append(result('files.upload_route', 'pass' if uploaded['indexing_status']=='indexed' else 'fail',
                'Загрузка и обработка файла через API', 'Файл загружен в одноразовую базу, рабочие записи и разрешения не менялись.',
                {'source_count': 1, 'indexed': int(uploaded['indexing_status']=='indexed'),
                 'http_status': uploaded['staff_original_http_status']}))
            checks.append(result('files.access', 'pass' if uploaded['guest_original_http_status']==404 else 'fail',
                'Закрытый оригинал', 'После разрешения ответов оригинал по-прежнему закрыт для гостя.',
                {'http_status': uploaded['guest_original_http_status']}))
            checks += negative_route_checks(client, factory)
            with factory() as session:
                corpus='\n'.join(session.scalars(select(KnowledgeChunk.text).where(KnowledgeChunk.document_id==uploaded['document_id'])))
            vocabulary=(('ремень',r'ремень|ремни|ремня'),('датчик',r'датчик'),('конвейер',r'конвейер'),('скотч',r'скотч'),
                ('лента',r'лент'),('безопасность',r'безопасност'),('клапан',r'клапан'),('настройка',r'настройк'))
            topic=next((word for word, pattern in vocabulary if re.search(pattern,corpus,re.I)), None)
            if topic is None:
                from server.knowledge import _terms
                topic=next((word for word,_ in Counter(_terms(corpus)).most_common()
                    if word.isalpha() and 5 <= len(word) <= 18), 'SELFTEST')
            model=material['model'] if material else 'PE38-12'
            found=client.get('/api/knowledge/search', params={'q': model+' '+topic}).json().get('results', [])
            checks.append(result('knowledge.actual_search', 'pass' if found else 'fail', 'Поиск загруженного материала',
                'Проверен реальный API поиска; искусственный контрольный файл не считается вашей базой знаний.',
                {'source_count': len(found)}, '' if found else 'Проверьте распознанный текст и точную модель документа.'))
            private['material']=uploaded
            if not live:
                checks.append(result('model.live', 'skipped', 'Настоящая модель', 'Внешние вызовы отключены или ключ отсутствует.',
                    resolution='Для проверки модели запустите check.bat -Live с настроенным локальным .env.'))
                return checks, private, recorder
            # Compare the same short conversation without corporate retrieval.
            direct=run_direct([('basic', list(BASIC_TURNS))], recorder, model=models['main'], configured=True)
            private['direct']=direct
            direct_rows=direct.get('scenarios',[{}])[0].get('turns',[])
            direct_ok=len(direct_rows)==len(BASIC_TURNS) and all(r.get('status')=='returned' and r.get('provider_response_id') for r in direct_rows)
            direct_failure=next((row for row in direct_rows if row.get('status')!='returned'), {})
            checks.append(result('model.direct', 'pass' if direct_ok else 'fail', 'Модель напрямую',
                'Выполнены реальные запросы без поиска компании и сценария приложения.',
                {'model': models['main'], 'expected_turns': len(BASIC_TURNS),
                 'successful_turns': sum(r.get('status')=='returned' for r in direct_rows),
                 **(direct_failure.get('provider_diagnostics') or {}),
                 **({'error_code':direct_failure['error_code']} if direct_failure.get('error_code') else {})},
                '' if direct_ok else 'Причина прямого отказа провайдера указана в деталях. Проверка приложения всё равно различит свой этап ошибки.'))
            start_context=len(recorder.contexts)
            rows=application_turns(client, recorder, 'basic', list(BASIC_TURNS))
            private['basic']=rows
            checks += evaluate_turns('basic', rows, recorder.contexts[start_context:], expected_turns=len(BASIC_TURNS))
            if rows[-1].get('engine')!='openai':
                checks.append(result('model.technical', 'skipped', 'Технические вопросы',
                    'Первый разговорный тест приложения не прошёл. Дальнейшие платные ошибки не повторяются.'))
                return checks, private, recorder
            start_context=len(recorder.contexts)
            questions=[f'Модель {model}. При отключённом питании проверил видимый разъём: он закреплён. Что ещё нужно уточнить?',
                       'Модель и проверку уже сообщил. Объясни предыдущий ответ проще.',
                       'Почему ты это спрашиваешь?']
            technical=application_turns(client, recorder, 'technical', questions)
            private['technical']=technical
            checks += evaluate_turns('technical', technical, recorder.contexts[start_context:], expected_turns=len(questions))
            # The client-facing state deliberately omits internal check notes.
            # Inspect the owned disposable record, not the public projection.
            from server.models import ConversationState
            with factory() as session:
                saved=session.get(ConversationState, technical[-1].get('conversation_id'))
                remembered=dict(saved.state or {}) if saved is not None else {}
            attempts=remembered.get('attempted_actions') or []
            private['technical_memory']={'equipment_model':remembered.get('known_facts',{}).get('equipment_model'),
                                         'attempted_actions':attempts}
            memory_ok=(remembered.get('known_facts',{}).get('equipment_model')==model
                       and any(questions[0] in str(attempt) for attempt in attempts))
            checks.append(result('model.memory', 'pass' if memory_ok else 'fail',
                'Оборудование и выполненная проверка в памяти',
                'В сохранённом состоянии после уточняющих реплик остались модель и исходное сообщение о проверке разъёма. Это не оценка смысла всех ответов.',
                {'model': model, 'attempted_checks_count': len(attempts)}))
            if found:
                sourced=application_turns(client, recorder, 'source', [f'{model}: объясни по загруженному руководству сведения про {topic}. Укажи источник.'])
                private['source']=sourced
                source_ok=sourced[0].get('engine')=='openai' and bool(sourced[0].get('sources'))
                checks.append(result('model.grounded', 'pass' if source_ok else 'fail' if sourced[0].get('engine')!='openai' else 'warn', 'Ответ с источником',
                    'Проверены вызов и наличие ссылки; инженерная правильность всех рекомендаций автоматически не удостоверяется.',
                    {'source_count': len(sourced[0].get('sources',[])), **(sourced[0].get('provider_diagnostics') or {})},
                    '' if source_ok else 'Проверьте этап ошибки или недостаточность найденного материала в приватном отчёте.'))
            symptom=application_turns(client, recorder, 'customer_symptom', [CUSTOMER_SYMPTOM])
            private['customer_symptom']=symptom
            checks.append(result('model.customer_symptom', 'pass' if symptom[0].get('engine')=='openai' else 'fail',
                'Вопрос об остановке привода', 'Проверен реальный запрос с описанием симптома без выдуманной модели машины.',
                symptom[0].get('provider_diagnostics') or {},
                'Сам текст симптома не устанавливает причину поломки; ручное вмешательство в движущиеся узлы недопустимо.'))
    return checks, private, recorder


def main(argv=None):
    parser=argparse.ArgumentParser(description='Проверка POSTEKS: модель, файлы и история почты; без изменения рабочей базы.')
    parser.add_argument('--live', action='store_true', help='Реальные ограниченные вызовы OpenAI из локального .env.')
    parser.add_argument('--synthetic-only', action='store_true',
        help='Использовать искусственный документ в тестовом чате; рабочие документы проверяются только локально.')
    parser.add_argument('--mail-sample', action='store_true', help='Прочитать не более двух писем через IMAP без импорта/изменения флагов.')
    parser.add_argument('--output', type=Path, default=ROOT/'reports')
    args=parser.parse_args(argv)
    from scripts.selfcheck_report import write_reports
    checks, private, metadata = [], {}, {'created_at': datetime.now(timezone.utc).isoformat(), 'live_requested': args.live}
    stage='configuration'
    def add(items):
        checks.extend(items)
        for item in items:
            print(f"[{item['status'].upper()}] {item['title']}", flush=True)
    try:
        from server import main as application, ai
        from server.config import settings
        from server.models import AppSetting
        metadata.update(python_version=__import__('platform').python_version(), max_output_tokens=settings.max_ai_output_tokens,
            key_configured=bool(settings.openai_api_key))
        try: metadata['openai_sdk_version']=version('openai')
        except PackageNotFoundError: metadata['openai_sdk_version']='missing'
        add([check_citation_validation()])
        stage='working_database'
        with working_database() as session:
            models=ai.model_config(session) if session is not None else {
                'main':settings.openai_model,'fast':settings.openai_fast_model,'vision':settings.openai_vision_model,
                'audio':settings.openai_audio_model,'embedding':settings.openai_embedding_model}
            metadata.update(model=models['main'], model_origin='saved_settings' if session is not None and session.get(AppSetting,'models') else 'env_defaults')
            add([result('model.config', 'pass' if settings.openai_api_key else 'fail' if args.live else 'warn',
                'Настройки модели', 'Показаны имя действующей модели и наличие ключа, без значения ключа.',
                {'model':models['main'],'configured':bool(settings.openai_api_key)},
                '' if settings.openai_api_key else 'Задайте OPENAI_API_KEY в локальном .env и перезапустите проверку. Ключ в отчёт не входит.')])
            stage='local_files_and_index'
            local, material=local_checks(session);add(local)
            if args.synthetic_only:
                material = None
            metadata['test_material_kind']='real' if material else 'synthetic'
            stage='mail_history'
            from scripts.check_mail_history import check_mail_history
            if session is not None:
                add(check_mail_history(session, live=args.mail_sample, sample_limit=2))
            else:
                add([result('mail.history','skipped','История почты','Рабочая база недоступна для проверки контрольных точек.')])
        stage='isolated_file_and_model_checks'
        model_checks, model_private, recorder=exercise_application(models, material,
            live=args.live and bool(settings.openai_api_key))
        add(model_checks);private.update(model_private)
        private['provider_requests']=recorder.requests
        metadata.update(provider_calls_started=sum(r.get('provider_call_started') is True for r in recorder.requests),
            input_tokens=sum(int(r.get('input_tokens') or 0) for r in recorder.requests),
            output_tokens=sum(int(r.get('output_tokens') or 0) for r in recorder.requests))
    except KeyboardInterrupt:
        add([result('runner.interrupted','warn','Проверка прервана','Сохранены результаты уже завершённых шагов. Полнота не заявляется.')])
    except Exception as exc:
        add([result('runner.error','fail','Проверка остановилась на внутренней ошибке',
            'Тип и этап ошибки сохранены без содержимого конфиденциальных данных.', {'exception_type':type(exc).__name__, 'stage':stage},
            'Передайте support_report.json. .env, рабочую БД и PRIVATE_details.json отправлять не нужно.')])
    paths=write_reports(args.output, checks, metadata, private_details=private)
    print('Отчёт: '+paths['html'], flush=True)
    print('Для поддержки: '+paths['support'], flush=True)
    return 1 if any(c['status']=='fail' for c in checks) else 2 if any(c['status'] in {'warn','skipped'} for c in checks) else 0


if __name__=='__main__':
    raise SystemExit(main())
