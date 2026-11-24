import json
from time import monotonic
from types import SimpleNamespace

import httpx

from test_conversation_model import natural, post, strict
from test_core import env
from server import web_sources


MANUAL_URL = 'https://www.veichi.com/docs/sd700.pdf'
EXCERPT = 'Er.810 — synthetic alarm description for this test.'


def context(sources=None):
    return {
        'message': 'Модель Veichi SD700. ЭНКОДЕР. PRIVATE_CLIENT_MARKER',
        'state': {'known_facts': {}, 'goal': 'Ошибка энкодера Er.810'},
        'current_turn': {'component_model': 'Veichi SD700', 'alarm_code': 'Er.810', 'web_lookup_allowed': True},
        'sources': sources or [],
    }


def test_search_uses_only_fixed_public_fields_and_prefers_matching_local_source():
    query = web_sources.manual_query(context([{'model': 'PE38-12', 'text': EXCERPT}]))
    assert query.search == 'VEICHI SD700 Er.810 encoder alarm troubleshooting'
    assert web_sources.manual_query(context([{'model': 'VEICHI SD700', 'text': EXCERPT}])) is None
    assert web_sources.manual_query(context([{'model': 'VEICHI SD7000', 'text': EXCERPT}])) == query
    assert web_sources.manual_query(context([{'model': 'VEICHI SD700', 'text': 'Synthetic index.'}])) == query


def test_external_urls_redirect_and_extract_only_allowed_document():
    domains = ('veichi.com',)
    visited = []

    def handle(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://veichi.com.evil.invalid/sd700'})

    query = web_sources.manual_query(context())
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert web_sources._get_document(client, MANUAL_URL, domains, monotonic() + 5) is None
    assert visited == [MANUAL_URL]
    assert web_sources.official_url('https://veichi.com@localhost/sd700', domains) is None
    assert web_sources.official_url('https://veichi.com:8443/sd700', domains) is None
    assert web_sources._sources_from_document('https://veichi.com/manual?model=SD700', EXCERPT, query, monotonic() + 5) == []
    assert web_sources._sources_from_document(MANUAL_URL, EXCERPT, query, monotonic() + 5)[0]['text'] == EXCERPT


def test_pdf_evidence_keeps_the_original_page():
    import fitz

    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 80), 'VEICHI SD700 test manual')
        pdf.new_page().insert_text((72, 80), 'Er.810. Synthetic alarm, no service action.')
        content = pdf.tobytes()
    query = web_sources.manual_query(context())
    sources = web_sources._sources_from_document('https://veichi.com/manual.pdf', content, query, monotonic() + 5)
    assert len(sources) == 1
    assert sources[0]['page'] == 2
    assert sources[0]['text'] == 'Er.810. Synthetic alarm, no service action.'


def test_web_source_is_grounded_and_survives_public_history(strict, monkeypatch):
    import openai
    from server.config import settings

    monkeypatch.setattr(settings, 'web_search_enabled', True)
    monkeypatch.setattr(settings, 'web_search_domains', 'veichi.com')
    calls = []

    class SDK:
        def __init__(self, **kwargs):
            self.responses = self

        def create(self, **kwargs):
            calls.append(kwargs)
            usage = SimpleNamespace(input_tokens=10, output_tokens=15)
            if kwargs.get('tools'):
                return SimpleNamespace(usage=usage, output=[{
                    'type': 'message', 'content': [{'annotations': [
                        {'type': 'url_citation', 'url': MANUAL_URL},
                        {'type': 'url_citation', 'url': 'https://bad.invalid/foo'},
                    ]}],
                }])
            body = json.loads(kwargs['input'])
            assert body['web_lookup_status'] == 'found'
            assert body['sources'][0]['url'] == MANUAL_URL
            reply = natural(body, 'Описание диагностического сообщения приведено в руководстве [1].',
                            technical=True, quote=EXCERPT)
            return SimpleNamespace(status='completed', id='resp_web_fixture', output=[],
                                   output_text=json.dumps(reply), usage=usage)

        def close(self):
            pass

    def document(_client, url, _domains, _deadline):
        assert url == MANUAL_URL
        return url, EXCERPT

    monkeypatch.setattr(openai, 'OpenAI', SDK)
    monkeypatch.setattr(web_sources, '_get_document', document)
    answer = post(strict.client, 'Veichi SD700. Ошибка Er.810, нужна инструкция по энкодеру. PRIVATE_CLIENT_MARKER')
    assert answer['engine'] == 'openai'
    assert answer['message']['sources'][0]['source_type'] == 'official_web'
    assert answer['message']['sources'][0]['url'] == MANUAL_URL
    assert answer['usage'] == {'input_tokens': 20, 'output_tokens': 30}
    assert len(calls) == 2 and calls[0]['input'] == 'VEICHI SD700 Er.810 encoder alarm troubleshooting'
    assert 'PRIVATE_CLIENT_MARKER' not in json.dumps(calls[0])
    assert calls[0]['tools'][0]['filters'] == {'allowed_domains': ['veichi.com']}
    history = strict.client.get('/api/conversations/' + answer['conversation_id']).json()['messages']
    assert history[-1]['sources'][0]['url'] == MANUAL_URL
