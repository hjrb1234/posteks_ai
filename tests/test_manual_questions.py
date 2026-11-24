"""Synthetic long manuals test evidence selection, never a memorized belt answer."""
import hashlib
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.db import Base
from server.models import Document, DocumentPage, KnowledgeChunk
from server import workflows, mail_models  # noqa: F401


@pytest.fixture
def manuals(monkeypatch):
    from server.config import settings
    monkeypatch.setattr(settings, 'openai_api_key', '')
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def manual(session, model, pages, *, visibility='internal', unread=None):
    identity = uuid.uuid4().hex
    item = Document(title='TEST: руководство ' + model, filename=model + '.pdf', storage_name=identity + '.pdf',
                    sha256=hashlib.sha256(identity.encode()).hexdigest(), mime_type='application/pdf',
                    size_bytes=1, models=[model], visibility=visibility, status='indexed',
                    pages=len(pages), chunks=len(pages), indexing_details={
                        'total': len(pages), 'pages_with_text': len(pages) - len(unread or []),
                        'extraction_complete': not unread, 'unread_pages': unread or []})
    session.add(item)
    session.flush()
    for index, text in enumerate(pages, 1):
        session.add(DocumentPage(document_id=item.id, page=index, text=text, layout={
            'ocr_status': 'unavailable' if index in (unread or []) else 'not_required'}))
        if text:
            session.add(KnowledgeChunk(document_id=item.id, page=index, text=text, models=[model]))
    session.commit()
    return item


def test_plural_parts_question_finds_inventory_late_in_long_exact_model_manual(manuals):
    from server.manual_context import inspect_manuals
    pages = ['TEST: общая вводная информация.'] * 80
    pages[68] = 'TEST: Перечень деталей узла захвата. Ремень зубчатый TEST-ALPHA-DRIVE. Замена по износу.'
    wanted = manual(manuals, 'PE38-25', pages)
    foreign = manual(manuals, 'PE38-12', ['TEST: Ремни только TEST-OTHER-MACHINE. Перечень деталей.'])
    result = inspect_manuals(manuals, 'PE38-25: какие ремни предусмотрены у захвата?', model='PE38-25', include_internal=True)
    assert result['available'] and result['coverage']['pages_scanned'] == 80
    assert any(source['document_id'] == wanted.id and source['page'] == 69 for source in result['sources'])
    assert not any(source['document_id'] == foreign.id for source in result['sources'])
    assert 'TEST-OTHER-MACHINE' not in str(result)


def test_absent_marking_is_not_a_proof_that_a_part_is_never_used(manuals):
    from server.manual_context import inspect_manuals
    manual(manuals, 'PE38-25', ['TEST: Перечень деталей. Ремень зубчатый TEST-ALPHA-DRIVE.'])
    result = inspect_manuals(manuals, 'PE38-25: подходит ли HTD 9000 3M 19?', model='PE38-25', include_internal=True)
    assert result['parts_check']['requested_markings']
    assert result['parts_check']['unmatched_markings']
    assert result['parts_check']['absence_proves_incompatibility'] is False


def test_unknown_variant_cannot_borrow_a_known_neighbouring_manual(manuals):
    from server.manual_context import inspect_manuals
    manual(manuals, 'PE38-25', ['TEST: ремень TEST-ALPHA-DRIVE для PE38-25.'])
    result = inspect_manuals(manuals, 'На 38-35 подходят эти ремни?', model='PE38-35', include_internal=True)
    assert result['model'] == 'PE38-35'
    assert result['available'] is False and result['sources'] == []
    assert not result['coverage']['documents']


def test_missing_ocr_page_prevents_full_manual_coverage_claim(manuals):
    from server.manual_context import inspect_manuals
    manual(manuals, 'PE38-25', ['TEST: Ремень TEST-ALPHA-DRIVE.', '', 'TEST: руководство.'], unread=[2])
    result = inspect_manuals(manuals, 'PE38-25 ремни?', model='PE38-25', include_internal=True)
    assert result['available']
    assert result['coverage']['complete'] is False
    assert result['coverage']['documents'][0]['unread_pages'] == [2]
    assert result['parts_check']['absence_proves_incompatibility'] is False


def test_internal_manual_stays_private_even_for_matching_model(manuals):
    from server.manual_context import inspect_manuals
    item = manual(manuals, 'PE38-25', ['TEST: Ремень TEST-PRIVATE-MARKING.'])
    public = inspect_manuals(manuals, 'PE38-25 ремни', model='PE38-25', include_internal=False)
    assert public['sources'] == [] and not public['available']
    assert 'TEST-PRIVATE-MARKING' not in str(public)
    private = inspect_manuals(manuals, 'PE38-25 ремни', model='PE38-25', include_internal=True)
    assert private['available'] and any(source['document_id'] == item.id for source in private['sources'])


def test_shared_parts_inventory_is_not_mistaken_for_variant_parameter_columns(manuals):
    from server.orchestrator import respond
    item = manual(manuals, 'PE38-25', ['TEST: Перечень деталей. Ремень TEST-WIDE шириной 25 мм.'])
    item.models = ['PE38-17', 'PE38-25']
    page = manuals.query(DocumentPage).filter_by(document_id=item.id).one()
    page.layout = {'tables': [{'headers': ['Поз.', 'Обозначение', 'Наименование', 'Кол-во'],
        'rows': [['Поз.', 'Обозначение', 'Наименование', 'Кол-во'],
                 ['1', 'TEST-WIDE', 'Ремень шириной 25 мм', '1']]}]}
    manuals.commit()
    output = respond(manuals, 'PE38-25: какие ремни перечислены?', equipment_model='PE38-25',
                     include_internal=True, allow_actions=False)
    assert output['action'] == 'answer'
    assert 'TEST-WIDE' in output['content']
    assert 'В общем руководстве' in output['content']
    assert 'не подтверждение комплектации' in output['content']
    assert output['sources'][0]['table_kind'] == 'parts_inventory'


def test_bare_unknown_model_is_recognized_without_aliasing_to_known_variant():
    from server.knowledge import detect_models, normalize_model
    assert detect_models('Формирователь 38-35: нужна инструкция') == ['PE38-35']
    assert normalize_model('38-35') == 'PE38-35'
    assert normalize_model('38-35') != normalize_model('38-25')


def test_tray_filename_revision_is_not_a_different_machine_model(manuals):
    from server.knowledge import detect_models
    from server.manual_context import inspect_manuals
    item = manual(manuals, 'PE5310', ['TEST: заготовка движется к прессу; проверка датчика.'])
    item.title = 'Руководство формирователя лотков с эл схемой 5310_3'
    item.filename = 'Руководство_формирователя_лотков_с_эл_схемой_5310_3.pdf'
    item.models = []
    manuals.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == item.id)).models = []
    manuals.commit()
    assert detect_models(item.filename) == ['PE5310']
    result = inspect_manuals(manuals, 'PE5310: заготовка не доезжает к прессу', model='PE5310', include_internal=True)
    assert result['available'] and result['coverage']['pages_scanned'] == 1
    assert any(source['document_id'] == item.id for source in result['sources'])


def test_plural_belts_retrieval_repairs_legacy_assembly_model_tags(manuals):
    from server.knowledge import search_knowledge
    item = manual(manuals, 'PE38-25', ['TEST: Перечень деталей. Ремень зубчатый TEST-ALPHA-DRIVE.'])
    chunk = manuals.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == item.id))
    chunk.models = ['PE3805-441', 'PE3812-800']
    manuals.commit()
    results = search_knowledge(manuals, 'PE38-25: ремни?', model='PE38-25', include_internal=True)
    assert results and results[0]['document_id'] == item.id
    assert results[0]['models'] == ['PE38-25']


def test_matching_only_a_drawing_number_is_not_parts_evidence(manuals):
    from server.knowledge import search_knowledge
    from server.manual_context import inspect_manuals
    manual(manuals, 'PE38-25', ['9000. TEST: общая информация о предприятии и сервисе.'])
    query = 'PE38-25: подходит ли HTD 9000 3M 19?'
    assert search_knowledge(manuals, query, model='PE38-25', include_internal=True) == []
    result = inspect_manuals(manuals, query, model='PE38-25', include_internal=True)
    assert result['coverage']['pages_scanned'] == 1
    assert result['sources'] == []
    assert result['parts_check']['absence_proves_incompatibility'] is False


def test_request_to_reach_into_running_machine_does_not_return_adjustment_quotes(manuals):
    from server.orchestrator import respond
    manual(manuals, 'PE38-25', ['TEST: При замятии короба измените положение направляющих.'])
    result = respond(manuals, 'PE38-25: можно рукой поправить застрявший короб, не выключая машину?',
                     equipment_model='PE38-25', include_internal=True, allow_actions=False)
    assert 'останов' in result['content'].casefold() or 'отключ' in result['content'].casefold()
    assert 'измените положение направляющих' not in result['content'].casefold()
