"""Paid audio requests are bounded; transport cleanup cannot deadlock the AI."""
import io
from types import SimpleNamespace
import wave

from test_core import env
from server.config import settings
from server.models import AIUsage
from server import media
from sqlalchemy import select


def wav(seconds=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\0\0' * int(8000 * seconds))
    return output.getvalue()


def test_audio_duration_rejects_long_input_before_api(env, monkeypatch):
    import openai
    monkeypatch.setattr(settings, 'openai_api_key', 'test-only-key')
    def forbidden(**kwargs):
        raise AssertionError('Long recordings must not construct an API client')
    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    response = env.client.post('/api/audio/transcribe', files={'file': ('speech.wav', wav(121), 'audio/wav')})
    assert response.status_code == 422 and '2 минут' in response.json()['detail']


def test_audio_metering_and_cleanup_failure_do_not_lock_following_requests(env, monkeypatch):
    import openai
    monkeypatch.setattr(settings, 'openai_api_key', 'test-only-key')
    calls = []
    class Client:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(retrieve=lambda model: None)
            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='Проверьте маркировку PE38-12', usage=None)
        def close(self):
            raise RuntimeError('Synthetic cleanup failure')
    monkeypatch.setattr(openai, 'OpenAI', Client)
    for _ in range(2):
        response = env.client.post('/api/audio/transcribe', files={'file': ('speech.wav', wav(), 'audio/wav')})
        assert response.status_code == 200, response.text
        assert response.json()['text'].startswith('Проверьте')
        assert response.json()['duration_seconds'] == 1
        assert any('20000' in warning for warning in response.json()['warnings'])
    assert len(calls) == 2
    with env.factory() as session:
        rows = list(session.scalars(select(AIUsage)))
        assert sum(row.input_tokens for row in rows) == 40000


def test_compressed_audio_reports_required_parser_without_api_calls(env, monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'test-only-key')
    monkeypatch.setattr(media.shutil, 'which', lambda name: None)
    response = env.client.post('/api/audio/transcribe', files={'file': ('speech.ogg', b'OggS' + b'\0' * 30, 'audio/ogg')})
    assert response.status_code == 503 and 'ffprobe' in response.json()['detail']
