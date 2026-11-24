"""Bounded attachment memory read from messages of an already owned dialogue.

Callers must authenticate conversation ownership before passing Conversation.
The optional scope is an additional guard; an attachment can never migrate
between conversations merely because its identifier is mentioned in text.
"""
from __future__ import annotations
from copy import deepcopy
import re

from fastapi import HTTPException
from sqlalchemy import String, cast, literal, select

from .models import AppSetting, ChatMessage, Conversation
from .media import _attachment_dto


def conversation_attachment_context(session, conversation: Conversation | None,
                                    current_attachments=None, *, scope: str | None = None,
                                    max_items=12, text_budget=12000):
    current_attachments = list(current_attachments or [])[-12:]
    if conversation is None:
        # Dry-run callers have already checked their uploads. No conversation
        # or account-wide lookup is performed in this mode.
        return _bounded(current_attachments, max_items, text_budget)
    if scope is not None and scope != conversation.owner_scope:
        raise HTTPException(404, 'Диалог не найден')
    expected_owner = conversation.owner_scope.removeprefix('playground:')
    message_key = literal('chat_message_attachments:') + cast(ChatMessage.id, String)
    records = list(session.execute(select(AppSetting.value, ChatMessage.id).join(
        ChatMessage, AppSetting.key == message_key).where(
        ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.id.desc()).limit(100)))
    ordered = {}
    # Canonical records are checked again: deleted/unbound/other-dialogue files
    # do not regain access through a stale snapshot in a prior chat message.
    snapshots = [(value.get('attachments') or [], message_id) for value, message_id in reversed(records)]
    snapshots.append((current_attachments, None))
    current_ids = {str(value.get('id')) for value in current_attachments}
    for attachments, message_id in snapshots:
        for snapshot in attachments:
            identifier = str(snapshot.get('id') or '')
            if not re.fullmatch('[a-f0-9]{32}', identifier):
                continue
            record = session.get(AppSetting, 'chat_attachment:' + identifier)
            value = record.value if record else None
            if not value:
                # Missing content is reported explicitly; do not forward old
                # extracted text as if the original still existed/was verified.
                item = {'id': identifier, 'filename': str(snapshot.get('filename') or 'Вложение')[:240],
                    'kind': snapshot.get('kind'), 'received': True, 'processing_status': 'missing',
                    'content_processed': False, 'text': '', 'warnings': ['Вложение было получено, но запись его содержимого сейчас недоступна.']}
            elif value.get('owner_scope') != expected_owner or value.get('conversation_id') != conversation.id:
                continue
            else:
                item = _attachment_dto(value)
            item['current_turn'] = identifier in current_ids
            item['source_message_id'] = message_id or ordered.get(identifier, {}).get('source_message_id')
            ordered.pop(identifier, None)
            ordered[identifier] = item
    return _bounded(list(ordered.values()), max_items, text_budget)


def _bounded(items, max_items, text_budget):
    result = []
    count = min(max(int(max_items), 0), 24)
    if count == 0:
        return result
    remaining = min(max(int(text_budget), 0), 24000)
    for source in reversed(items[-count:]):
        item = {key: deepcopy(source[key]) for key in (
            'id', 'filename', 'kind', 'url', 'engine', 'received', 'processing_status',
            'content_processed', 'analysis_kind', 'processing_error_code', 'video',
            'current_turn', 'source_message_id') if key in source}
        item['warnings'] = [str(value)[:600] for value in (source.get('warnings') or [])[:8]]
        text = str(source.get('text') or source.get('analysis_text') or '')
        if source.get('content_processed') is False or source.get('processing_status') in {'failed', 'missing', 'received', 'metadata_only'}:
            text = ''
        item['text'] = text[:min(remaining, 4000)]
        remaining -= len(item['text'])
        result.append(item)
    return list(reversed(result))
