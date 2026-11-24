"""Real video containers and private dialogue memory, without simulated vision."""
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_core import env
from server import media
from server.attachment_context import conversation_attachment_context
from server.models import AppSetting, ChatMessage, Conversation


@pytest.fixture
def video_bytes(tmp_path):
    binary = shutil.which('ffmpeg')
    if not binary or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg and ffprobe required for video integration checks')
    path = tmp_path / 'real-video.mp4'
    completed = subprocess.run([binary, '-v', 'error', '-nostdin', '-f', 'lavfi',
        '-i', 'color=c=blue:s=64x48:r=10', '-t', '1.0', '-c:v', 'mpeg4', '-y', str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    return path.read_bytes()


def bind_as_sent(env, identifier):
    """Persist the same canonical/message links as the actual chat endpoint."""
    with env.factory() as session:
        row = session.get(AppSetting, 'chat_attachment:' + identifier)
        owner = row.value['owner_scope']
        conversation = Conversation(id=str(uuid.uuid4()), owner_scope=owner, title='TEST: оборудование')
        session.add(conversation)
        session.flush()
        attachments = media.owned_attachments(session, [identifier], owner, conversation.id)
        message = ChatMessage(conversation_id=conversation.id, role='user', content='Модель PE38-12, видео прилагаю.', sources=[])
        session.add(message)
        session.flush()
        session.add(AppSetting(key='chat_message_attachments:' + str(message.id), value={'attachments': attachments}))
        session.commit()
        return conversation.id, owner, message.id


def test_real_video_is_received_but_metadata_is_not_fictitious_analysis(env, video_bytes):
    response = env.client.post('/api/attachments', files={'file': ('machine.mp4', video_bytes, 'video/mp4')})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['kind'] == 'video' and data['received'] is True
    assert data['processing_status'] == 'metadata_only' and data['content_processed'] is False
    assert data['processing_error_code'] == 'missing_vision_key'
    assert data['text'] == '' and data['analysis_text'] == ''
    assert data['video']['width'] == 64 and data['video']['height'] == 48
    assert data['video']['duration_seconds'] > 0
    assert data['video']['sample_times_seconds'] == []
    assert data['video']['audio_analyzed'] is False
    assert env.client.get(data['url']).content == video_bytes


def test_real_sample_frames_are_extracted_and_not_called_full_video_analysis(env, video_bytes):
    path = env.tmp_path / 'sample.mp4'
    path.write_bytes(video_bytes)
    metadata, code, warning = media._video_probe(path)
    assert code is None and warning is None
    frames = media._video_frames(path, metadata)
    assert len(frames) == 3
    assert [time for time, _ in frames] == sorted(time for time, _ in frames)
    from PIL import Image
    import io
    for _, image in frames:
        with Image.open(io.BytesIO(image)) as frame:
            assert frame.width == 960 and frame.height == 720
    assert metadata['audio_analyzed'] is False


def test_single_frame_video_has_one_honest_sample_instead_of_false_failure(env):
    binary = shutil.which('ffmpeg')
    if not binary or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg and ffprobe required for video integration checks')
    path = env.tmp_path / 'one-frame.mp4'
    made = subprocess.run([binary, '-v', 'error', '-nostdin', '-f', 'lavfi',
        '-i', 'color=c=blue:s=64x48:r=25', '-frames:v', '1', '-c:v', 'mpeg4', '-y', str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert made.returncode == 0
    metadata, code, _ = media._video_probe(path)
    assert code is None
    frames = media._video_frames(path, metadata)
    assert len(frames) == 1 and frames[0][0] == 0.0


def test_video_probe_failure_preserves_original_and_reports_received_failed(env, video_bytes, monkeypatch):
    monkeypatch.setattr(media, '_video_probe', lambda _: (None, 'video_probe_failed', 'TEST: видео получено, обработка не удалась.'))
    response = env.client.post('/api/attachments', files={'file': ('broken-machine.mp4', video_bytes, 'video/mp4')})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['received'] is True and data['processing_status'] == 'failed'
    assert not data['content_processed'] and not data['text']
    assert data['warnings'] and env.client.get(data['url']).content == video_bytes
    conversation_id, owner, message_id = bind_as_sent(env, data['id'])
    with env.factory() as session:
        previous = conversation_attachment_context(session, session.get(Conversation, conversation_id), scope=owner)
        assert len(previous) == 1 and previous[0]['id'] == data['id']
        assert previous[0]['source_message_id'] == message_id and not previous[0]['current_turn']
        assert previous[0]['processing_status'] == 'failed' and not previous[0]['text']


def test_video_without_tools_has_actionable_warning_and_preserves_original(env, monkeypatch):
    monkeypatch.delenv('FFPROBE_PATH', raising=False)
    monkeypatch.delenv('FFMPEG_PATH', raising=False)
    monkeypatch.setattr(media, '_IS_WINDOWS', False)
    monkeypatch.setattr(media.shutil, 'which', lambda _: None)
    upload = b'\x00\x00\x00\x14ftypmp42'
    response = env.client.post('/api/attachments', files={'file': ('sample.mp4', upload, 'video/mp4')})
    assert response.status_code == 200
    payload = response.json()
    assert payload['processing_status'] == 'failed' and payload['content_processed'] is False
    assert payload['processing_error_code'] == 'video_probe_unavailable'
    assert 'FFPROBE_PATH' in payload['warnings'][0]
    assert env.client.get(payload['url']).content == upload


def test_video_reports_invalid_decoder_configuration_before_provider_call(env, video_bytes, monkeypatch):
    monkeypatch.setenv('FFMPEG_PATH', str(env.tmp_path / 'unavailable-ffmpeg'))
    monkeypatch.setattr(media.settings, 'openai_api_key', 'fixture-only')
    monkeypatch.setattr(media, '_video_observations', lambda *_: pytest.fail('Should not call OpenAI'))
    response = env.client.post('/api/attachments', files={'file': ('sample.mp4', video_bytes, 'video/mp4')})
    assert response.status_code == 200
    payload = response.json()
    assert payload['video']['width'] == 64
    assert payload['processing_error_code'] == 'video_frames_configuration'
    assert 'FFMPEG_PATH' in payload['warnings'][0]
    assert str(env.tmp_path) not in payload['warnings'][0]
    assert env.client.get(payload['url']).content == video_bytes


def test_video_probe_uses_container_duration_when_stream_duration_is_unavailable(env, monkeypatch):
    monkeypatch.setattr(media, '_ffmpeg_binary', lambda _: 'fixture-ffprobe')
    monkeypatch.setattr(media.subprocess, 'run', lambda *_args, **_kwargs: subprocess.CompletedProcess(
        'fixture-ffprobe', 0, b'{"streams":[{"width":64,"height":48,"duration":"N/A"}],"format":{"duration":"2.0"}}'))
    metadata, code, _ = media._video_probe(env.tmp_path / 'sample.webm')
    assert code is None and metadata['duration_seconds'] == 2.0


def test_video_tools_find_windows_winget_links_and_check_programs_run(env, monkeypatch):
    monkeypatch.setattr(media, '_IS_WINDOWS', True)
    monkeypatch.delenv('FFMPEG_PATH', raising=False)
    monkeypatch.delenv('FFPROBE_PATH', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(env.tmp_path))
    directory = env.tmp_path / 'Microsoft' / 'WinGet' / 'Links'
    probe, encoder = map(lambda name: str(directory / name), ('ffprobe.exe', 'ffmpeg.exe'))
    monkeypatch.setattr(media.shutil, 'which', lambda path: path if path in (probe, encoder) else None)
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[0] == encoder))

    monkeypatch.setattr(media.subprocess, 'run', run)
    assert media.video_tool_status() == {'ffprobe': 'ready', 'ffmpeg': 'unusable'}
    assert calls == [[probe, '-version'], [encoder, '-version']]


def test_conversation_remembers_file_without_resending_and_rejects_other_owner(env):
    response = env.client.post('/api/attachments', files={'file': ('plate.txt', 'Модель PE38-12. Датчик уже проверили.'.encode(), 'text/plain')})
    assert response.status_code == 200
    attached = response.json()
    conversation_id, owner, message_id = bind_as_sent(env, attached['id'])
    with env.factory() as session:
        conversation = session.get(Conversation, conversation_id)
        memory = conversation_attachment_context(session, conversation, scope=owner)
        assert memory[0]['id'] == attached['id']
        assert 'Датчик уже проверили' in memory[0]['text']
        assert memory[0]['source_message_id'] == message_id
        # Mentioning another conversation's identifier never grants access.
        other = Conversation(id=str(uuid.uuid4()), owner_scope=owner, title='TEST: другой диалог')
        session.add(other)
        session.commit()
        assert conversation_attachment_context(session, other, [attached], scope=owner) == []
        with pytest.raises(HTTPException) as error:
            conversation_attachment_context(session, conversation, scope='guest:someone-else')
        assert error.value.status_code == 404


def test_unsent_upload_is_not_implicitly_added_to_dialogue_memory(env):
    response = env.client.post('/api/attachments', files={'file': ('sent.txt', b'TEST: SENT', 'text/plain')})
    attached = response.json()
    conversation_id, owner, _ = bind_as_sent(env, attached['id'])
    pending = env.client.post('/api/attachments', data={'conversation_id': conversation_id},
        files={'file': ('pending.txt', b'TEST: PENDING MUST NOT ENTER THE NEXT MESSAGE', 'text/plain')})
    assert pending.status_code == 200
    with env.factory() as session:
        memory = conversation_attachment_context(session, session.get(Conversation, conversation_id), scope=owner)
        assert len(memory) == 1 and memory[0]['id'] == attached['id']
        assert 'PENDING' not in str(memory)


def test_missing_attachment_record_is_not_replaced_with_stale_extracted_content(env):
    response = env.client.post('/api/attachments', files={'file': ('saved.txt', b'TEST: OLD CONTENT', 'text/plain')})
    attached = response.json()
    conversation_id, owner, _ = bind_as_sent(env, attached['id'])
    with env.factory() as session:
        session.delete(session.get(AppSetting, 'chat_attachment:' + attached['id']))
        session.commit()
        memory = conversation_attachment_context(session, session.get(Conversation, conversation_id), scope=owner)
        assert memory[0]['processing_status'] == 'missing'
        assert not memory[0]['text'] and 'OLD CONTENT' not in str(memory)


def test_attachment_memory_budget_uses_newest_content_first():
    values = [{'id': str(index), 'text': str(index) * 10000, 'processing_status': 'processed',
               'content_processed': True, 'filename': f'{index}.txt'} for index in range(20)]
    memory = conversation_attachment_context(None, None, values, max_items=4, text_budget=5000)
    assert len(memory) == 4 and sum(len(item['text']) for item in memory) == 5000
    assert len(memory[-1]['text']) == 4000
    assert all('analysis_text' not in item and 'owner_scope' not in item for item in memory)
    assert conversation_attachment_context(None, None, values, max_items=0) == []


def test_video_extension_does_not_accept_unrelated_file_contents(env):
    response = env.client.post('/api/attachments', files={'file': ('program.mp4', b'MZ' + b'not-a-video' * 4, 'video/mp4')})
    assert response.status_code == 422
    with env.factory() as session:
        assert session.scalar(select(AppSetting).where(AppSetting.key.startswith('chat_attachment:'))) is None
