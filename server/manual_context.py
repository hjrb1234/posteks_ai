"""Inspect all authorized pages of the exact manual, then select evidence.

Scanning text is not a claim that a language model read every page, that OCR is
complete, or that an absent catalogue entry proves incompatibility. Those limits
are deliberately included in the provider context and staff trace.
"""
from collections import Counter
import math
import re

from sqlalchemy import select

from .knowledge import _model_matches, _terms, detect_models, normalize_model
from .models import AppSetting, Document, DocumentPage
from .source_access import can_use_for_answer, source_access_metadata

# Vocabulary bridges descriptions of symptoms to words used by manuals. These
# are retrieval hints, never stored machine specifications or canned answers.
ALIASES = (
    (r'рем[её]н|ремн|пассик|пасик|\bhtd\b|\b[58]\s*m\b', 'ремень ремни привод конвейер зубчатый перечень детали'),
    (r'мн[её]т|мят[ыь]|смят|жует|ж[уё]ет|ломает.{0,20}короб|рв[её]т.{0,20}картон', 'короб гофрокороб деформация направляющие неисправность'),
    (r'присоск|не\s+(?:бер[её]т|подхват)|заготовк.{0,25}(?:оста|пад|бер)', 'присоска вакуум захват заготовка неисправность'),
    (r'кос[оы]|перекос|набок|крив[оа]|уводит', 'перекос направляющие гофрокороб регулировка'),
    (r'скотч|лент[аыу]|закле|липн|липк', 'клейкая лента заклеивающая группа заклейка неисправность'),
    (r'дно|донн|клапан|лепест', 'клапаны нижние дно короб складывание'),
    (r'лоток|лотк|кле[йя]|не\s+скле', 'лоток заготовка клей клеевой неисправность'),
    (r'встал|стоит|замер|не\s+(?:движ|едет|пуска|запуска)|останов', 'остановка пуск датчик неисправность'),
    (r'поменял|друг[оа].{0,20}(?:короб|размер)|перенастро|размер', 'размер гофрокороб настройка регулировка'),
)
PARTS = re.compile(r'рем[её]н|ремн|пассик|пасик|\bhtd\b|совместим|подойд[её]т|запчаст|перечень.{0,12}детал', re.I)
_NON_TECHNICAL = {'какой','какие','используются','использу','машин','машине','нужен','нужны','можно','пожалуйст','инструкц','руководств','перечн','детал','формировател','работает','работа','вопрос','сказ','подойд','подходит'}


def expanded_query(query):
    return query + ' ' + ' '.join(extra for pattern, extra in ALIASES if re.search(pattern, query, re.I))


def _query_terms(query):
    stripped = list(query)
    for start, end, _ in _model_matches(query):
        stripped[start:end] = ' ' * (end - start)
    return set(_terms(''.join(stripped))) - _NON_TECHNICAL


def _markings(query):
    values = []
    # Capture markings/length-pitch families independently of their brand. The
    # exact raw user value is kept; no replacement length is calculated.
    pattern = r'(?<!\d)\d{3,5}\s*(?:[-/]\s*)?(?:(?:HTD\s*)?[358]\s*M|HTD)(?:\s*[-xх×/]?\s*\d{1,3})?'
    for match in re.finditer(pattern, query, re.I):
        value = match.group(0).strip()
        if value.casefold() not in {v.casefold() for v in values}:
            values.append(value)
    return values[:12]


def _fold_marking(value):
    return re.sub(r'[^a-zа-я0-9]', '', value.casefold())


def _window(text, terms, size=2200):
    if len(text) <= size:
        return text
    lines = text.splitlines(keepends=True)
    offset, best, best_score = 0, 0, -1
    for line in lines:
        score = len(set(_terms(line)) & terms)
        if score > best_score:
            best, best_score = offset, score
        offset += len(line)
    start = max(0, min(best - 350, len(text) - size))
    return text[start:start + size]


def _document_models(document):
    return {normalize_model(v) for v in (document.models or [])} or set(detect_models(document.title + ' ' + document.filename))


def _inventory_rows(page, query_terms):
    """Bind ordinary parts-table cells without inventing per-variant columns.

    A four-column position/designation/name/count table is a shared parts list,
    not a performance table whose separate columns name machine variants.
    Unrecognized/continued tables retain the conservative original-page path.
    """
    selected = []
    for table in (page.layout or {}).get('tables', []):
        rows = table.get('rows') or []
        candidates = [table.get('headers') or []] + rows[:2]
        header = next((r for r in candidates if len(r) == 4
            and 'поз' in str(r[0]).casefold()
            and 'обознач' in str(r[1]).casefold()
            and 'наимен' in str(r[2]).casefold()
            and re.search(r'кол', str(r[3]), re.I)), None)
        if not header:
            continue
        for row in rows:
            if len(row) != 4 or row == header:
                continue
            text = ' | '.join(f'{label}: {str(value or "").strip()}' for label, value in zip(
                ('Позиция', 'Обозначение', 'Наименование', 'Количество'), row))
            if query_terms & set(_terms(text)):
                selected.append(text)
    return '\n'.join(selected[:12])[:2200]


def inspect_manuals(session, query, model=None, document_id=None, include_internal=False, limit=6):
    query = str(query or '')[:6000]
    inferred = detect_models(query)
    machine = normalize_model(model) if model else (inferred[0] if len(inferred) == 1 else None)
    report = {'model': machine, 'available': False,
        'coverage': {'documents': [], 'pages_scanned': 0, 'matching_pages': 0, 'complete': False,
                     'scope': 'indexed_text_only', 'drawings_understood': False},
        'sources': [], 'parts_check': {'requested_markings': _markings(query), 'matched_markings': [],
            'unmatched_markings': [], 'absence_proves_incompatibility': False},
        'notice': 'Отсутствие маркировки в тексте не доказывает несовместимость. Полнота OCR, модификация и таблицы требуют отдельной проверки.'}
    if not machine and document_id is None:
        return report
    if len(inferred) > 1 or (machine and inferred and machine not in inferred):
        report['notice'] = 'Выбранная и названная модели различаются. Руководства разных модификаций не объединялись.'
        return report
    history_ids = {r.value.get('document_id') for r in session.scalars(select(AppSetting).where(AppSetting.key.startswith('history_import:')))}
    statement = select(Document).where(Document.deleted.is_(False), Document.status == 'indexed')
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    documents = [d for d in session.scalars(statement)
        if can_use_for_answer(d, include_internal)
        and d.id not in history_ids
        and (d.indexing_details or {}).get('source_kind') != 'unreviewed_history'
        and not re.search(r'непроверен|история переписки', d.category or '', re.I)
        and (not machine or machine in _document_models(d))]
    if not documents:
        report['notice'] = 'Нет разрешённого руководства именно этой модели. Нельзя подменять её похожей модификацией.'
        return report
    report['available'] = True
    primary = _query_terms(query)
    expanded = _query_terms(expanded_query(query))
    parts = bool(PARTS.search(query))
    ranked = []
    all_text = []
    for document in documents:
        pages = list(session.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page)))
        details = document.indexing_details or {}
        unread = set(details.get('unread_pages') or [])
        unread.update(p.page for p in pages if not p.text.strip())
        unread.update(set(range(1, document.pages + 1)) - {p.page for p in pages})
        complete = bool(pages and len(pages) == document.pages and not unread and details.get('extraction_complete') is not False)
        report['coverage']['documents'].append({'document_id': document.id, 'title': document.title,
            'total_pages': document.pages, 'text_pages': sum(bool(p.text.strip()) for p in pages),
            'unread_pages': sorted(unread), 'extraction_complete': complete})
        report['coverage']['pages_scanned'] += len(pages)
        for page in pages:
            text = page.text or ''
            all_text.append(text)
            counts = Counter(_terms(text))
            matched = expanded & counts.keys()
            # Digits, units or model identifiers alone cannot select a page.
            meaningful = {t for t in matched if not t.isdigit() and t not in {'мм','шт','htd','8m','5m'}}
            if not meaningful:
                continue
            primary_hits = primary & meaningful
            score = sum((3 if t in primary_hits else 1) * (1 + math.log(1 + counts[t])) for t in meaningful)
            inventory = bool(re.search(r'наименование|перечень|спецификац|обозначение|количество|кол[-.]?\s*во', text, re.I))
            if parts and 'ремень' in counts:
                score += 15 if inventory else 6
            if parts and not ('ремень' in counts or any(_fold_marking(m) in _fold_marking(text) for m in report['parts_check']['requested_markings'])):
                score *= .2
            if not primary_hits:
                score *= .65
            page_models = set(detect_models(text)) & _document_models(document)
            source = {'document_id': document.id, 'page': page.page, 'title': document.title,
                'record_version': document.version, 'source_fingerprint': document.sha256,
                'filename': document.filename, 'source_type': 'document', 'manual_inspection': True,
                'model': machine or ', '.join(sorted(_document_models(document))),
                'models': sorted(page_models or _document_models(document)),
                'text': _window(text, primary or expanded), 'score': round(score, 4),
                'category': document.category,
                'url': f'/api/documents/{document.id}/file#page={page.page}',
                **source_access_metadata(document, include_internal)}
            inventory_text = _inventory_rows(page, primary or expanded) if parts else ''
            if inventory_text:
                source['text'] = inventory_text
                source['table_kind'] = 'parts_inventory'
                source['table_binding'] = 'shared_manual' if len(_document_models(document)) > 1 else 'single_model'
            source['excerpt'] = source['text'][:1200]
            ranked.append(source)
    ranked.sort(key=lambda r: (-r['score'], r['document_id'], r['page']))
    report['sources'] = ranked[:min(max(limit, 0), 12)]
    report['coverage']['matching_pages'] = len(ranked)
    report['coverage']['complete'] = all(d['extraction_complete'] for d in report['coverage']['documents'])
    combined = _fold_marking('\n'.join(all_text))
    requested = report['parts_check']['requested_markings']
    report['parts_check']['matched_markings'] = [m for m in requested if _fold_marking(m) in combined]
    report['parts_check']['unmatched_markings'] = [m for m in requested if _fold_marking(m) not in combined]
    return report
