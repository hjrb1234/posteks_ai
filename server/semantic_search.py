"""Optional, explicitly built semantic retrieval for authorized verified knowledge.

Only source identifiers, content fingerprints and vectors are cached. Original
text and permissions are read afresh on every search. Raw mail is never indexed.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_admin, require_staff
from .config import settings
from .db import get_session, utcnow
from .knowledge import normalize_model, detect_models
from .models import AIUsage, AppSetting, Document, KnowledgeChunk
from .source_access import can_use_for_answer, client_answer_predicate, source_access_metadata

INDEX_KEY = 'semantic:index:v1'
MAX_CORPUS = 500
MAX_VECTOR = 4096
MIN_SIMILARITY = .45
router = APIRouter(prefix='/api/knowledge/semantic')


def _hash(source):
    value = {key: source.get(key) for key in ('key', 'title', 'text', 'model', 'models')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _history_ids(session):
    return {row.value.get('document_id') for row in session.scalars(
        select(AppSetting).where(AppSetting.key.startswith('history_import:')))}


def _allowed_document(document, include_internal, history_ids):
    return bool(can_use_for_answer(document, include_internal)
        and (document.indexing_details or {}).get('source_kind') != 'unreviewed_history'
        and document.id not in history_ids
        and 'непроверен' not in (document.category or '').casefold()
        and 'история переписки' not in (document.category or '').casefold())


def _solution_source(row):
    return {'key': 's:' + str(row.id), 'solution_id': row.id, 'source_type': 'verified_solution',
        'title': row.title, 'text': f'Симптомы: {row.symptoms}\nПроверенные действия: {row.actions}\nОграничения: {row.limitations}',
        'model': normalize_model(row.equipment_model) if row.equipment_model else '',
        'url': f'/api/knowledge/solutions/{row.id}'}


def _chunk_source(chunk, document, include_internal=False):
    declared = document.models or detect_models(document.title + ' ' + document.filename)
    models = [model for model in (chunk.models or []) if model in declared] or declared
    return {'key': 'c:' + str(chunk.id), 'document_id': document.id, 'chunk_id': chunk.id,
        'source_type': 'document', 'title': document.title, 'filename': document.filename,
        'text': chunk.text, 'excerpt': chunk.text[:1200], 'page': chunk.page,
        'models': models, 'model': ', '.join(models),
        'category': document.category, 'url': f'/api/documents/{document.id}/file#page={chunk.page}',
        **source_access_metadata(document, include_internal)}


def _corpus(session, include_internal):
    from .workflows import VerifiedSolution
    statement = select(VerifiedSolution).where(VerifiedSolution.status == 'VERIFIED').order_by(VerifiedSolution.id)
    if not include_internal:
        statement = statement.where(VerifiedSolution.visibility == 'public')
    sources = [_solution_source(row) for row in session.scalars(statement.limit(MAX_CORPUS + 1))]
    scan_truncated = False
    if len(sources) <= MAX_CORPUS:
        history_ids = _history_ids(session)
        statement = select(KnowledgeChunk, Document).join(Document).where(Document.deleted.is_(False),
            Document.status.in_(['indexed', 'partial'])).order_by(KnowledgeChunk.id)
        if not include_internal:
            statement = statement.where(client_answer_predicate())
        # Bound iteration even if an administrator imported a very large archive.
        for scanned, (chunk, document) in enumerate(session.execute(statement.limit(MAX_CORPUS * 4 + 1)), 1):
            scan_truncated = scanned > MAX_CORPUS * 4
            if _allowed_document(document, include_internal, history_ids):
                sources.append(_chunk_source(chunk, document, include_internal))
                if len(sources) > MAX_CORPUS:
                    break
    return sources[:MAX_CORPUS], len(sources) > MAX_CORPUS or scan_truncated


def _resolve(session, key, include_internal, history_ids=None):
    from .workflows import VerifiedSolution
    try:
        kind, raw_id = key.split(':', 1)
        object_id = int(raw_id)
    except (AttributeError, ValueError):
        return None
    if kind == 's':
        row = session.get(VerifiedSolution, object_id, populate_existing=True)
        if not row or row.status != 'VERIFIED' or (not include_internal and row.visibility != 'public'):
            return None
        return _solution_source(row)
    if kind == 'c':
        chunk = session.get(KnowledgeChunk, object_id, populate_existing=True)
        document = session.get(Document, chunk.document_id, populate_existing=True) if chunk else None
        if not _allowed_document(document, include_internal, history_ids if history_ids is not None else _history_ids(session)):
            return None
        return _chunk_source(chunk, document, include_internal)
    return None


def _normal_vector(value):
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= MAX_VECTOR:
        raise ValueError('Invalid vector dimensions')
    vector = [float(item) for item in value]
    if not all(math.isfinite(item) for item in vector):
        raise ValueError('Invalid vector values')
    norm = math.sqrt(sum(item * item for item in vector))
    if not norm:
        raise ValueError('Empty vector')
    return [item / norm for item in vector]


def _embed(session, texts, model):
    from . import ai
    if not settings.openai_api_key:
        raise HTTPException(503, 'OpenAI не настроен. Лексический поиск продолжает работать.')
    if not ai._ai_lock.acquire(blocking=False):
        raise HTTPException(429, 'ИИ занят. Повторите построение семантического индекса позднее.')
    client = None
    try:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        used = session.scalar(select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(AIUsage.created_at >= start)) or 0
        # Character count is a conservative upper bound used before a paid call.
        if used + sum(len(text) for text in texts) + 100 > settings.max_ai_daily_tokens:
            raise HTTPException(429, 'Недостаточно остатка дневного лимита. Выберите меньшую партию индексации.')
        session.commit()  # Never hold a SQLite INSERT during network I/O.
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, timeout=30, max_retries=1)
        client.models.retrieve(model)
        response = client.embeddings.create(model=model, input=texts)
        usage = getattr(response, 'usage', None)
        consumed = getattr(usage, 'prompt_tokens', 0) or getattr(usage, 'total_tokens', 0) or sum(len(text) for text in texts)
        session.add(AIUsage(model=model, input_tokens=consumed, output_tokens=0))
        session.commit()  # Meter an invalid model response too.
        data = sorted(response.data, key=lambda item: item.index)
        if len(data) != len(texts) or [item.index for item in data] != list(range(len(texts))):
            raise ValueError('Invalid embedding response count')
        vectors = [_normal_vector(item.embedding) for item in data]
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError('Inconsistent embedding dimensions')
        return vectors
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(502, ai.safe_api_error(exc)) from None
    finally:
        try:
            close = getattr(client, 'close', None)
            if close:
                close()
        except Exception:
            pass
        finally:
            ai._ai_lock.release()


def semantic_status(session):
    from .ai import model_config
    model = model_config(session)['embedding']
    row = session.get(AppSetting, INDEX_KEY)
    index = row.value if row else {}
    corpus, truncated = _corpus(session, True)
    entries = index.get('entries', []) if index.get('model') == model else []
    valid, indexed_public = [], 0
    history_ids = _history_ids(session)
    for entry in entries[:MAX_CORPUS]:
        source = _resolve(session, entry.get('key'), True, history_ids)
        if not source or entry.get('hash') != _hash(source):
            continue
        try:
            _normal_vector(entry.get('vector'))
        except (TypeError, ValueError, OverflowError):
            continue
        valid.append(entry)
        indexed_public += bool(_resolve(session, entry.get('key'), False, history_ids))
    return {'configured': bool(settings.openai_api_key), 'model': model, 'indexed': len(valid),
        'indexed_public': indexed_public, 'eligible_up_to_limit': len(corpus),
        'pending_up_to_limit': max(0, len(corpus) - len(valid)), 'stale': max(0, len(index.get('entries', [])) - len(valid)),
        'ready': bool(valid and settings.openai_api_key), 'complete': len(corpus) == len(valid) and not truncated,
        'truncated': truncated, 'max_corpus': MAX_CORPUS, 'updated_at': index.get('updated_at'),
        'minimum_similarity': MIN_SIMILARITY,
        'index_model': index.get('model'), 'last_error': index.get('error'),
        'notice': 'Сырые письма и непроверенные решения исключены. Близость текста не подтверждает совместимость деталей. Без готового индекса используется текстовый поиск.'}


class RebuildBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include_internal: bool = False
    max_items: int = Field(default=50, ge=1, le=MAX_CORPUS)


@router.get('/status')
def status(user=Depends(require_staff), session: Session = Depends(get_session)):
    return semantic_status(session)


@router.post('/rebuild')
def rebuild(body: RebuildBody, user=Depends(require_admin), session: Session = Depends(get_session)):
    from .ai import model_config
    from .workflows import record_event
    model = model_config(session)['embedding']
    corpus, truncated = _corpus(session, body.include_internal)
    row = session.get(AppSetting, INDEX_KEY)
    index = row.value if row and row.value.get('model') == model else {}
    previous = {entry.get('key'): entry for entry in index.get('entries', [])}
    current = {source['key']: source for source in corpus}
    kept = {key: previous[key] for key, source in current.items() if key in previous and previous[key].get('hash') == _hash(source)}
    pending = [source for source in corpus if source['key'] not in kept][:body.max_items]
    if not pending:
        return {'built': 0, 'remaining_up_to_limit': max(0, len(corpus) - len(kept)), **semantic_status(session)}
    try:
        vectors = _embed(session, [(source['title'] + '\n' + source['text'])[:6000] for source in pending], model)
    except HTTPException as exc:
        # Keep the last valid index and persist a safe operational explanation.
        # A failed rebuild must never be reported as a newly completed index.
        session.rollback()
        row = session.get(AppSetting, INDEX_KEY, populate_existing=True)
        value = dict(row.value) if row else {'model': model, 'entries': []}
        value['error'] = str(exc.detail)[:500]
        if row:
            row.value = value
        else:
            session.add(AppSetting(key=INDEX_KEY, value=value))
        session.commit()
        raise
    # Another staff member may revoke/alter a source during the network call.
    history_ids = _history_ids(session)
    for source, vector in zip(pending, vectors):
        fresh = _resolve(session, source['key'], body.include_internal, history_ids)
        if fresh and _hash(fresh) == _hash(source):
            kept[source['key']] = {'key': source['key'], 'hash': _hash(source), 'vector': vector}
    value = {'model': model, 'entries': list(kept.values()), 'updated_at': utcnow().isoformat() + 'Z', 'error': None,
             'include_internal': body.include_internal, 'truncated': truncated}
    row = session.get(AppSetting, INDEX_KEY, populate_existing=True)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=INDEX_KEY, value=value))
    record_event(session, user.username, 'semantic.rebuild', 'knowledge', 'semantic', {'built': len(pending), 'include_internal': body.include_internal})
    session.commit()
    return {'built': len(pending), 'remaining_up_to_limit': max(0, len(corpus) - len(kept)), **semantic_status(session)}


def search_semantic(session, query, *, include_internal=False, equipment_model=None, limit=6,
                    kinds=None, document_id=None):
    """Return only fresh, currently permitted sources; failures preserve lexical search."""
    from .ai import model_config
    if not settings.openai_api_key or not str(query or '').strip():
        return []
    row = session.get(AppSetting, INDEX_KEY)
    model = model_config(session)['embedding']
    if not row or row.value.get('model') != model:
        return []
    candidates = []
    history_ids = _history_ids(session)
    machine = normalize_model(equipment_model) if equipment_model else None
    for entry in list(row.value.get('entries', []))[:MAX_CORPUS]:
        source = _resolve(session, entry.get('key'), include_internal, history_ids)
        if not source or entry.get('hash') != _hash(source):
            continue
        if kinds and source['source_type'] not in kinds:
            continue
        if document_id is not None and source.get('document_id') != document_id:
            continue
        source_models = {normalize_model(value) for value in (source.get('models') or ([source.get('model')] if source.get('model') else []))}
        if machine and machine not in source_models:
            continue
        try:
            vector = _normal_vector(entry.get('vector'))
        except (ValueError, TypeError, OverflowError):
            continue
        candidates.append((entry, source, vector))
    if not candidates:
        return []
    try:
        query_vector = _embed(session, [str(query)[:4000]], model)[0]
    except HTTPException:
        return []
    results = []
    history_ids = _history_ids(session)
    for entry, source, vector in candidates:
        if len(vector) != len(query_vector):
            continue
        score = sum(left * right for left, right in zip(vector, query_vector))
        if score < MIN_SIMILARITY:
            continue
        fresh = _resolve(session, source['key'], include_internal, history_ids)
        if not fresh or _hash(fresh) != entry['hash']:
            continue
        fresh.pop('key', None)
        fresh['score'], fresh['retrieval'] = round(score, 6), 'semantic'
        results.append(fresh)
    results.sort(key=lambda source: -source['score'])
    return results[:min(max(int(limit), 0), 12)]
