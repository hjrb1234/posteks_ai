"""Staff mail views use correspondence, real dates, and local read state only."""
from datetime import datetime
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from test_core import env, login
from server import workflows as wf
from server.mail_models import MailRemoteMessage
from server.workflows import AuditEvent, InboxMessage, ServiceCase


@pytest.fixture
def mail_env(env, monkeypatch):
    # Never inherit the operator's mailbox identities or any live credentials.
    for key in ('IMAP_USER', 'IMAP_USERNAME', 'MAIL_ADDRESS', 'CORPORATE_EMAIL_DOMAINS',
                'KB_RESPONSE_SENDERS', 'DEPARTMENT_SALES_EMAIL',
                'DEPARTMENT_ASU_EMAIL', 'DEPARTMENT_PRODUCTION_EMAIL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MAIL_USERNAME', 'service@posteks.ru')
    monkeypatch.setenv('CORPORATE_EMAIL_DOMAIN', 'posteks.ru')
    monkeypatch.setenv('DEPARTMENT_KB_EMAIL', 'engineer@posteks.ru')
    monkeypatch.setenv('DEPARTMENT_MANAGER_EMAIL', 'sales@posteks.ru')
    monkeypatch.setenv('BUSINESS_TIMEZONE', 'Asia/Yekaterinburg')
    login(env.client)
    return env


def message(identity, sender='person@client.example', recipient='service@posteks.ru',
            date='Thu, 22 Oct 2026 09:00:00 +0000', refs=None, body='Добрый день! Нужна информация.'):
    mail = EmailMessage()
    mail['From'] = sender
    mail['To'] = recipient
    mail['Subject'] = 'TEST: запрос ' + identity
    mail['Message-ID'] = '<' + identity + '@tests.invalid>'
    mail['Date'] = date
    if refs:
        mail['References'] = mail['In-Reply-To'] = '<' + refs + '@tests.invalid>'
    mail.set_content(body)
    return mail.as_bytes()


def upload(client, *messages):
    result = client.post('/api/mail/import', files=[
        ('files', (f'test-{i}.eml', raw, 'message/rfc822')) for i, raw in enumerate(messages)
    ])
    assert result.status_code == 200, result.text
    assert result.json()['imported'] == len(messages), result.text


def items(client, **params):
    response = client.get('/api/mail/inbox', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_clients_internal_outgoing_are_separate_and_private(mail_env):
    client = mail_env.client
    upload(client,
        message('client'),
        message('staff', sender='engineer@posteks.ru'),
        message('sent', sender='service@posteks.ru', recipient='person@client.example'),
        message('forwarded', sender='service@posteks.ru', recipient='sales@posteks.ru'),
    )
    assert [m['sender'] for m in items(client)['items']] == ['person@client.example']
    internal = items(client, category='internal')['items']
    assert {m['message_id'] for m in internal} == {'<staff@tests.invalid>', '<forwarded@tests.invalid>'}
    assert next(m for m in internal if m['sender'] == 'engineer@posteks.ru')['department'] == 'Конструкторское бюро'
    sent = items(client, category='outgoing')['items']
    assert len(sent) == 1 and sent[0]['direction'] == 'outgoing' and sent[0]['is_new'] is False
    assert items(client, category='all')['total'] == 4
    client.post('/api/auth/logout')
    assert client.get('/api/mail/inbox').status_code == 401
    assert client.get('/api/mail/customers').status_code == 401


def test_real_mail_dates_control_order_ranges_and_thread_not_import_id(mail_env):
    client = mail_env.client
    # Import newest first, then older to make numeric IDs misleading.
    upload(client,
        message('new', date='Fri, 23 Oct 2026 10:00:00 +0000', refs='old'),
        message('old', date='Fri, 16 Oct 2026 10:00:00 +0000'),
        # 22:30 UTC is the following day at the configured company timezone.
        message('boundary', date='Thu, 22 Oct 2026 22:30:00 +0000'),
    )
    newest = items(client)
    assert [m['message_id'] for m in newest['items']] == ['<new@tests.invalid>', '<boundary@tests.invalid>', '<old@tests.invalid>']
    assert [m['message_id'] for m in items(client, sort='oldest')['items']] == list(reversed([m['message_id'] for m in newest['items']]))
    assert items(client, date_from='2026-10-23', date_to='2026-10-23')['total'] == 2
    assert items(client, date_from='2026-10-22', date_to='2026-10-22')['total'] == 0
    response = client.get('/api/mail/inbox?date_from=not-a-date')
    assert response.status_code == 422
    thread = client.get(f"/api/inbox/{newest['items'][0]['id']}/thread").json()
    assert [m['message_id'] for m in thread['messages']] == ['<old@tests.invalid>', '<new@tests.invalid>']
    assert thread['case_kind'] == 'correspondence'
    assert thread['customer_key'] == 'domain:client.example'
    assert thread['customer_email'] == 'person@client.example'
    legacy = client.get('/api/inbox').json()
    assert [m['message_id'] for m in legacy] == [m['message_id'] for m in newest['items']]


def test_company_cards_group_business_domain_but_not_unrelated_public_mail(mail_env):
    client = mail_env.client
    upload(client,
        message('one', sender='one@client.example'),
        message('two', sender='two@client.example'),
        message('public-one', sender='one@mail.ru'),
        message('public-two', sender='two@mail.ru'),
    )
    groups = client.get('/api/mail/customers').json()
    assert {g['key'] for g in groups} == {'domain:client.example', 'email:one@mail.ru', 'email:two@mail.ru'}
    company = next(g for g in groups if g['key'] == 'domain:client.example')
    assert company['thread_count'] == company['message_count'] == company['new_count'] == 2
    assert items(client, customer_key='domain:client.example')['total'] == 2


def test_local_read_changes_only_selected_thread_and_never_imap_flags(mail_env, monkeypatch):
    client = mail_env.client
    upload(client, message('root'), message('reply', refs='root'), message('other'))
    rows = {m['message_id']: m for m in items(client)['items']}
    root, reply, other = (rows['<' + key + '@tests.invalid>'] for key in ('root', 'reply', 'other'))
    monkeypatch.setattr(wf.imaplib, 'IMAP4_SSL', lambda *a, **kw: pytest.fail('Local read must never connect to IMAP'))
    bad = client.post(f"/api/inbox/{root['id']}/read", json={'message_ids': [root['id'], other['id']]})
    assert bad.status_code == 422
    assert items(client)['unread_total'] == 3
    # Default is the selected message, not every message sharing a thread.
    good = client.post(f"/api/inbox/{root['id']}/read", json={})
    assert good.json() == {'updated': 1, 'read': True, 'imap_flags_changed': False}
    assert items(client)['unread_total'] == 2
    explicit = client.post(f"/api/inbox/{root['id']}/read", json={'message_ids': [root['id'], reply['id']]})
    assert explicit.json()['updated'] == 2
    assert items(client)['unread_total'] == 1
    with mail_env.factory() as session:
        assert session.get(InboxMessage, other['id']).local_read_at is None
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == 'mail.local_read').order_by(AuditEvent.id.desc()))
        assert event.detail['imap_flags_changed'] is False
        assert 'person@client.example' not in str(event.detail)


def test_imports_are_correspondence_until_explicit_promote_and_keep_one_case(mail_env):
    client = mail_env.client
    upload(client, message('client'), message('department', sender='engineer@posteks.ru'))
    inbox = items(client)['items'][0]
    assert inbox['case_kind'] == 'correspondence'
    assert client.get('/api/cases').json() == []
    assert len(client.get('/api/cases?kind=correspondence').json()) == 2
    assert client.get('/api/dashboard').json()['counts']['cases'] == 0
    promoted = client.post(f"/api/inbox/{inbox['id']}/promote")
    assert promoted.status_code == 200, promoted.text
    case = promoted.json()
    assert case['id'] == inbox['case_id'] and case['kind'] == 'service'
    assert case['email'] == 'person@client.example'
    again = client.post(f"/api/inbox/{inbox['id']}/promote")
    assert again.json()['id'] == case['id']
    assert [c['id'] for c in client.get('/api/cases').json()] == [case['id']]
    assert len(client.get('/api/cases?kind=correspondence').json()) == 1
    with mail_env.factory() as session:
        assert session.scalar(select(func.count()).select_from(ServiceCase)) == 2
        assert session.scalar(select(func.count()).select_from(InboxMessage)) == 2


def test_staff_and_sent_thread_resolve_customer_reply_address(mail_env):
    client = mail_env.client
    upload(client,
        message('customer', sender='buyer@factory.example'),
        message('kb', sender='engineer@posteks.ru', refs='customer'),
        message('answer', sender='service@posteks.ru', recipient='buyer@factory.example', refs='customer'),
    )
    all_messages = items(client, category='all')['items']
    assert {m['customer_key'] for m in all_messages} == {'domain:factory.example'}
    staff = next(m for m in all_messages if m['sender'] == 'engineer@posteks.ru')
    draft = client.post(f"/api/inbox/{staff['id']}/draft", json={'body': 'Здравствуйте! Отвечаем по вашему обращению.'})
    assert draft.status_code == 201, draft.text
    assert draft.json()['recipient'] == 'buyer@factory.example'


def test_history_records_not_new_and_pagination_does_not_hide_more_than_500(mail_env):
    client = mail_env.client
    with mail_env.factory() as session:
        for index in range(505):
            session.add(InboxMessage(message_id=f'<history-{index}@tests.invalid>', content_hash=f'{index:064x}',
                sender='archive@customer.example', recipients=['service@posteks.ru'], subject=f'TEST: архив {index}',
                body='Историческое письмо.', raw_mime=b'', source='IMAP_HISTORY', sent_at=datetime(2026, 1, 1)))
        session.commit()
    first = items(client, limit=200)
    assert first['total'] == 505 and len(first['items']) == 200 and first['has_more'] is True
    assert first['unread_total'] == 0 and all(not m['is_new'] for m in first['items'])
    last = items(client, limit=200, offset=400)
    assert len(last['items']) == 105 and last['has_more'] is False
    assert {m['id'] for m in first['items']}.isdisjoint(m['id'] for m in last['items'])


def test_remote_sent_folder_can_classify_alias_sender(mail_env):
    client = mail_env.client
    upload(client, message('alias', sender='alias@external.example', recipient='buyer@factory.example'))
    row = items(client)['items'][0]
    with mail_env.factory() as session:
        session.add(MailRemoteMessage(account_key='test', folder='Sent', uidvalidity='1', uid=1,
            inbox_id=row['id'], status='stored', direction='outgoing'))
        session.commit()
    assert items(client)['total'] == 0
    sent = items(client, category='outgoing')['items']
    assert len(sent) == 1 and sent[0]['customer_key'] == 'domain:factory.example'


def test_unthreaded_staff_message_does_not_adopt_an_unrelated_customer(mail_env):
    client = mail_env.client
    upload(client, message('unrelated'), message('unthreaded-staff', sender='engineer@posteks.ru'))
    with mail_env.factory() as session:
        for row in session.scalars(select(InboxMessage)):
            row.case_id = None
        session.commit()
    internal = items(client, category='internal')['items'][0]
    assert internal['customer_email'] == ''
    assert internal['customer_key'] == 'internal:engineer@posteks.ru'


def test_own_company_in_quoted_signature_is_not_customer_company(mail_env):
    client = mail_env.client
    upload(client, message('quoted-signature', body='Спасибо! Получили ответ.\n\n'
        '23.10.2026, 10:00, Сервис ПОСТЭКС <service@posteks.ru>:\n'
        'С уважением,\nООО «ПОСТЭКС»\nСлужба поддержки клиентов'))
    row = items(client)['items'][0]
    assert row['company_name'] == ''
    assert row['customer_label'] == 'person@client.example'
