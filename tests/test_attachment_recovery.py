from email.message import EmailMessage
from pathlib import Path
from sqlalchemy import select
from test_core import env, login
from server.mail_models import MailAttachment


def imported_attachment(env):
    login(env.client)
    message = EmailMessage()
    message['From'] = 'client@customer.example'
    message['To'] = 'service@company.example'
    message['Message-ID'] = '<recovery@tests.invalid>'
    message['Subject'] = 'TEST: инструкция'
    message.set_content('Пожалуйста, откройте вложение.')
    message.add_attachment(b'%PDF-1.4\nTEST ONLY\n%%EOF', maintype='application', subtype='pdf', filename='manual.pdf')
    response = env.client.post('/api/mail/import', files={'files': ('test.eml', message.as_bytes(), 'message/rfc822')})
    assert response.status_code == 200, response.text
    with env.factory() as session:
        item = session.scalar(select(MailAttachment))
        return item.id, Path(item.storage_path)


def test_relocated_windows_attachment_path_and_missing_cache_recovered_from_mime(env):
    identifier, path = imported_attachment(env)
    with env.factory() as session:
        session.get(MailAttachment, identifier).storage_path = 'C:\\old-install\\storage\\mail_attachments\\' + path.name
        session.commit()
    url = f'/api/mail/attachments/{identifier}/download'
    assert env.client.get(url).status_code == 200
    original = path.read_bytes()
    path.unlink()
    response = env.client.get(url)
    assert response.status_code == 200 and response.content == original
    assert path.read_bytes() == original
    env.client.post('/api/auth/logout')
    assert env.client.get(url).status_code == 401


def test_existing_corrupt_attachment_is_not_overwritten_or_served(env):
    identifier, path = imported_attachment(env)
    path.write_bytes(b'TAMPERED')
    response = env.client.get(f'/api/mail/attachments/{identifier}/download')
    assert response.status_code == 409
    assert path.read_bytes() == b'TAMPERED'
