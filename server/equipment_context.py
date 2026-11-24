"""Staff-only registry evidence: exact identification, never repair inference."""
from __future__ import annotations

import re
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_staff
from .db import get_session
from .knowledge import detect_models, normalize_model
from .workflows import Equipment, normalize_serial

router = APIRouter(prefix='/api/equipment', dependencies=[Depends(require_staff)])


def evidence(item: Equipment) -> dict:
    title = f'Реестр: {item.serial_number}'
    lines = [title, f'Серийный номер: {item.serial_number}', f'Модель: {item.model or "не указана"}',
             f'Заказчик: {item.customer or "не указан"}', f'Проект: {item.project or "не указан"}',
             f'Наименование: {item.name or "не указано"}',
             f'Дата отгрузки: {item.shipped_at or "не указана"}',
             'Гарантийный статус: неизвестен. Реестр подтверждает запись об оборудовании, '
             'но не совместимость деталей, параметры ремонта или действующую гарантию.']
    return {'id': item.id, 'title': title, 'serial_number': item.serial_number,
            'model': item.model, 'customer': item.customer, 'project': item.project,
            'shipped_at': item.shipped_at, 'name': item.name,
            'warranty_status': 'unknown', 'text': '\n'.join(lines)}


def _matches_model(item: Equipment, model: str | None) -> bool:
    if not model:
        return True
    wanted = normalize_model(model)
    return normalize_model(item.model) == wanted or wanted in detect_models(item.model)


def lookup(session: Session, query: str, *, model: str | None = None,
           facts: dict | None = None, limit: int = 8) -> list[Equipment]:
    """Exact serial/company only, with a labelled serial in free-form questions.

    Registry serial normalization deliberately preserves letter case and every
    character except spaces and hyphens, matching the workbook importer.
    """
    query = str(query or '').strip()[:6000]
    facts = facts or {}
    serials = {normalize_serial(query)} if query else set()
    if facts.get('serial_number'):
        serials.add(normalize_serial(facts['serial_number']))
    serial_match = re.search(
        r'(?:серийн\w*\s*(?:номер)?|s/n|serial)\s*[:№#]?\s*'
        r'([A-Za-zА-Яа-я0-9_/-]+(?:[ \t-][A-Za-zА-Яа-я0-9_/-]+){0,5})', query, re.I)
    if serial_match:
        # A labelled serial ends at punctuation, a new line, or the words of
        # the question. Enumerate token-prefixes, comparing each entire value.
        tokens = re.split(r'[ \t]+', serial_match.group(1))
        serials.update(normalize_serial(' '.join(tokens[:n])) for n in range(1, len(tokens) + 1))
    companies = {query.casefold()}
    if facts.get('company'):
        companies.add(str(facts['company']).strip().casefold())
    company_match = re.search(r'\b(?:ООО|АО|ПАО|ЗАО|ОАО|ИП)\s*[«"“][^»"”\n]{2,240}[»"”]', query)
    if company_match:
        companies.add(company_match.group(0).strip().casefold())
    companies = {company for company in companies if len(company) >= 3}
    # Customer casefold is evaluated in Python because SQLite lower() does not
    # support Cyrillic. No substring join, inferred owner or model-only match.
    rows = session.scalars(select(Equipment).order_by(Equipment.id))
    found = []
    for item in rows:
        if (item.normalized_serial in serials or
                (item.customer and item.customer.strip().casefold() in companies)) and _matches_model(item, model):
            found.append(item)
            if len(found) >= min(max(limit, 1), 8):
                break
    return found


def registry_sources(session: Session, query: str, *, facts: dict | None = None,
                     model: str | None = None) -> list[dict]:
    result = []
    for item in lookup(session, query, facts=facts, model=model):
        row = evidence(item)
        result.append({'equipment_id': item.id, 'source_type': 'equipment_registry',
                       'title': row['title'], 'text': row['text'], 'excerpt': row['text'],
                       'url': f'/api/equipment/{item.id}/evidence',
                       'model': '', 'models': [], 'equipment_model': item.model})
    return result


@router.get('/lookup')
def lookup_endpoint(q: str = Query(min_length=1, max_length=6000), model: str | None = None,
                    session: Session = Depends(get_session)):
    return [evidence(item) for item in lookup(session, q, model=model)]


@router.get('/{equipment_id}/evidence')
def evidence_endpoint(equipment_id: int, session: Session = Depends(get_session)):
    item = session.get(Equipment, equipment_id)
    if item is None:
        raise HTTPException(404, 'Оборудование не найдено')
    return evidence(item)
