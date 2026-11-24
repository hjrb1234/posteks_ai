"""Publication checks must reject private data while accepting non-secret limits."""
from pathlib import Path
from zipfile import ZipFile
from scripts.publication_guard import forbidden, inspect, inspect_zip


def test_environment_limits_are_not_credentials():
    template = b'OPENAI_API_KEY=\nMAIL_APP_PASSWORD=\nMAX_AI_DAILY_TOKENS=100000\n'
    assert inspect('.env.example', template) == []
    rejected = inspect('.env.example', b'OPENAI_API_KEY=not-a-real-key\n')
    assert rejected and 'not-a-real-key' not in str(rejected)


def test_release_archive_rejects_private_files_and_credentials(tmp_path: Path):
    archive = tmp_path / 'unsafe.zip'
    with ZipFile(archive, 'w') as output:
        output.writestr('posteks_ai/storage/posteks_ai.db', b'private database')
        output.writestr('posteks_ai/docs/TECHNICAL_SPEC.md', b'private original input')
        output.writestr('posteks_ai/server/leak.py', ('sk-' + 'A' * 35).encode())
    issues = inspect_zip(archive)
    assert any('posteks_ai.db' in issue for issue in issues)
    assert any('TECHNICAL_SPEC.md' in issue for issue in issues)
    assert any('possible credential' in issue for issue in issues)
    assert 'A' * 35 not in str(issues)


def test_mail_files_and_cross_platform_secret_paths_stay_private():
    for name in ['docs/real.eml', 'docs/inbox.mbox', 'docs/mail.msg', r'docs\.ENV', r'STORAGE\notes.txt',
                 r'docs\TECHNICAL_SPEC.md', 'docs/technical_spec.md']:
        assert forbidden(name)
    assert not forbidden('tests/fixtures/service-request.eml')
    assert forbidden('tests/fixtures/another.eml')


def test_recognizable_telegram_credentials_are_rejected_without_echoing_values():
    token = b'123456789:' + b'AA' + b'Q' * 33
    issues = inspect('server/accidental.txt', token)
    assert issues and 'possible credential' in issues[0]
    assert token.decode() not in str(issues)
