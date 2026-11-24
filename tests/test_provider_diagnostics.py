"""Recorded SDK fakes: classify provider failures without exposing private data.

No live OpenAI request occurs here. The shape of a real Responses result and
application schema are exercised so a successful HTTP call is not mistaken for
a valid technical answer.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_core import env
from server import ai
from server.config import settings
from server.models import AIUsage
from server.orchestrator import ActionPlan, initial_state, local_plan

PRIVATE = 'PRIVATE_PAYLOAD_KEY_RESPONSE_MUST_NOT_APPEAR'


@pytest.fixture
def configured(env, monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'TEST_NOT_A_REAL_PROVIDER_KEY')
    return env


def context():
    return {'message': 'Здравствуйте', 'state': initial_state(), 'sources': [],
        'recent_history': [], 'attachments': [], 'source_conflicts': [], 'manual_inspection': {},
        'current_turn': {'domain': 'technical', 'topic_change': False},
        'capabilities': {'internal_escalation': False, 'outbound_mail': False, 'booking': False}}


def result_for(value=None, **changes):
    value = local_plan(context()) if value is None else value
    result = SimpleNamespace(id='resp_test_diagnostics', _request_id='req_test_diagnostics',
        status='completed', output=[], output_text=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
        usage=SimpleNamespace(input_tokens=20, output_tokens=30,
                              output_tokens_details=SimpleNamespace(reasoning_tokens=12)))
    for key, item in changes.items():
        setattr(result, key, item)
    return result


def sdk(monkeypatch, response=None, error=None, cleanup_error=False):
    import openai
    observed = {'requests': [], 'metadata_lookups': 0}
    responses = iter(response) if isinstance(response, list) else None
    class Client:
        def __init__(self, **kwargs):
            self.models = self
            self.responses = self
        def retrieve(self, model):
            observed['metadata_lookups'] += 1
            raise RuntimeError(PRIVATE)
        def create(self, **kwargs):
            observed['requests'].append(kwargs)
            if error is not None:
                raise error
            if responses is not None:
                return next(responses)
            return response if response is not None else result_for()
        def close(self):
            if cleanup_error:
                raise RuntimeError(PRIVATE)
    monkeypatch.setattr(openai, 'OpenAI', Client)
    return observed


def generate(configured, payload=None):
    with configured.factory() as session:
        return ai.generate_plan(session, payload or context())


def assert_private_absent(metadata, caplog):
    assert PRIVATE not in json.dumps(metadata, ensure_ascii=False)
    assert PRIVATE not in caplog.text
    assert 'TEST_NOT_A_REAL_PROVIDER_KEY' not in caplog.text


def test_incomplete_output_is_not_misreported_as_invalid_json_or_api_transport(configured, monkeypatch, caplog):
    calls = sdk(monkeypatch, result_for(status='incomplete', incomplete_details=SimpleNamespace(reason='max_output_tokens')))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == 'response_output_limit'
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['stage'] == 'response' and diagnostic['provider_called'] is True
    assert diagnostic['response_status'] == 'incomplete' and diagnostic['incomplete_reason'] == 'max_output_tokens'
    assert diagnostic['response_id'] == 'resp_test_diagnostics'
    assert diagnostic['provider_request_id'] == 'req_test_diagnostics'
    assert diagnostic['reasoning_tokens'] == 12
    assert diagnostic['max_output_tokens'] == settings.max_ai_output_tokens
    assert len(calls['requests']) == 1, 'No hidden billable repair retry'
    with configured.factory() as session:
        usage = session.scalar(select(AIUsage))
        assert usage.input_tokens == 20 and usage.output_tokens == 30
    assert 'stage=response code=response_output_limit' in caplog.text


def test_json_parse_failure_retains_safe_response_identity_but_not_model_text(configured, monkeypatch, caplog):
    sdk(monkeypatch, result_for('{"response":"' + PRIVATE))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == 'response_json_invalid'
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['stage'] == 'parse' and diagnostic['exception_type'] == 'ValidationError'
    assert diagnostic['response_id'] == 'resp_test_diagnostics'
    assert diagnostic['validation_errors'][0]['type'] == 'json_invalid'
    assert metadata['usage'] == {'input_tokens': 20, 'output_tokens': 30}
    assert_private_absent(metadata, caplog)


def test_schema_error_does_not_expose_unknown_field_names_or_values(configured, monkeypatch, caplog):
    sdk(monkeypatch, result_for({'response': PRIVATE, PRIVATE: PRIVATE}))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == 'response_schema_invalid'
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['stage'] == 'parse'
    assert diagnostic['validation_errors'] and len(diagnostic['validation_errors']) <= 12
    assert_private_absent(metadata, caplog)


@pytest.mark.parametrize('changes,code', [
    ({'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': PRIVATE}]}], 'output_text': ''}, 'response_refused'),
    ({'output_text': ''}, 'response_empty'),
    ({'status': 'failed', 'error': {'code': 'server_error', 'message': PRIVATE}}, 'response_not_completed'),
    ({'status': 'incomplete', 'incomplete_details': {'reason': 'content_filter'}, 'output_text': PRIVATE}, 'response_incomplete'),
])
def test_no_usable_response_is_distinguished_without_leaking_body(configured, monkeypatch, caplog, changes, code):
    sdk(monkeypatch, result_for(**changes))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == code
    assert metadata['provider_diagnostics']['stage'] == 'response'
    assert_private_absent(metadata, caplog)


@pytest.mark.parametrize('claim,reason', [
    ({'source_index': 2, 'quote': PRIVATE, 'text': PRIVATE}, 'source_index_out_of_range'),
    ({'source_index': 1, 'quote': PRIVATE, 'text': PRIVATE}, 'quote_not_in_source'),
    ({'source_index': 1, 'quote': 'Достоверная выдержка.', 'text': PRIVATE}, 'claim_not_in_quote'),
    ({'source_index': 1, 'quote': '', 'text': ''}, 'quote_not_in_source'),
    ({'source_index': 1, 'quote': 'Достоверная выдержка.', 'text': ' '}, 'claim_not_in_quote'),
])
def test_citation_failure_is_an_evidence_error_after_a_successful_api_call(configured, monkeypatch, caplog, claim, reason):
    payload = context()
    payload['sources'] = [{'document_id': 123, 'page': 1, 'text': 'Достоверная выдержка.'}]
    proposed = local_plan(context())
    proposed['technical_claims'] = [claim]
    calls = sdk(monkeypatch, result_for(proposed))
    plan, metadata = generate(configured, payload)
    assert plan is None and metadata['error_code'] == 'citation_invalid'
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['stage'] == 'evidence' and diagnostic['citation_error'] == reason
    assert diagnostic['provider_called'] and diagnostic['response_status'] == 'completed'
    assert diagnostic['citation_retry']['citation_error'] == reason
    assert metadata['usage'] == {'input_tokens': 40, 'output_tokens': 60}
    assert len(calls['requests']) == 2
    assert_private_absent(metadata, caplog)


def test_source_whitespace_is_restored_in_validated_plan(configured, monkeypatch):
    payload = context()
    source = 'Примечание. Достоверная\nвыдержка из\u00a0руководства.'
    payload['sources'] = [{'document_id': 123, 'page': 5, 'text': source}]
    proposed = local_plan(context())
    proposed['technical_claims'] = [{'source_index': 1,
        'quote': 'Достоверная выдержка из руководства.', 'text': 'из руководства.'}]

    calls = sdk(monkeypatch, result_for(proposed))
    plan, metadata = generate(configured, payload)

    assert plan['technical_claims'] == [{'source_index': 1,
        'quote': 'Достоверная\nвыдержка из\u00a0руководства.', 'text': 'из\u00a0руководства.'}]
    assert metadata['provider_diagnostics']['stage'] == 'completed'
    assert len(calls['requests']) == 1
    assert json.loads(calls['requests'][0]['input'])['sources'][0] == {
        'document_id': 123, 'page': 5, 'text': source, 'source_index': 1,
    }
    assert 'source_index' not in payload['sources'][0]


def test_invalid_citation_is_repaired_once_with_same_context(configured, monkeypatch):
    monkeypatch.setattr(settings, 'reference_test_mode', False)
    proposed = local_plan(context())
    proposed['technical_claims'] = [{'source_index': 1, 'quote': PRIVATE, 'text': PRIVATE}]
    calls = sdk(monkeypatch, [result_for(proposed), result_for(id='resp_repaired')])

    response = configured.client.post('/api/chat', json={'message': 'Здравствуйте'})
    assert response.status_code == 200
    result = response.json()

    assert result['engine'] == 'openai' and result['message']['sources'] == []
    assert result['provider_response_id'] == 'resp_repaired'
    assert result['usage'] == {'input_tokens': 40, 'output_tokens': 60}
    assert result['provider_diagnostics']['citation_retry']['citation_error'] == 'source_index_out_of_range'
    first, second = calls['requests']
    assert first['input'] == second['input']
    assert 'Предыдущий план не прошёл проверку цитат' in second['instructions']
    assert PRIVATE not in json.dumps(second)
    history = configured.client.get('/api/conversations/' + result['conversation_id']).json()['messages']
    assert [message['role'] for message in history] == ['user', 'assistant']
    with configured.factory() as session:
        assert len(session.scalars(select(AIUsage)).all()) == 2


def test_citation_repair_respects_remaining_daily_budget(configured, monkeypatch):
    proposed = local_plan(context())
    proposed['technical_claims'] = [{'source_index': 1, 'quote': PRIVATE, 'text': PRIVATE}]
    calls = sdk(monkeypatch, result_for(proposed, usage=SimpleNamespace(
        input_tokens=settings.max_ai_daily_tokens, output_tokens=30)))

    plan, metadata = generate(configured)

    assert plan is None and metadata['error_code'] == 'citation_invalid'
    assert 'citation_retry' not in metadata['provider_diagnostics']
    assert len(calls['requests']) == 1


def provider_error(name, *, status=None, code=None, param=None):
    error = type(name, (Exception,), {})(PRIVATE)
    error.status_code = status
    error.request_id = 'req_test_failure'
    error.body = {'error': {'code': code, 'param': param, 'message': PRIVATE}}
    return error


@pytest.mark.parametrize('exception,code,kind', [
    (provider_error('AuthenticationError', status=401), 'authentication_failed', 'authentication'),
    (provider_error('PermissionDeniedError', status=403), 'model_access_denied', 'permission'),
    (provider_error('NotFoundError', status=404), 'model_not_found', 'model_not_found'),
    (provider_error('RateLimitError', status=429, code='insufficient_quota'), 'insufficient_quota', 'insufficient_quota'),
    (provider_error('APITimeoutError'), 'provider_timeout', 'timeout'),
    (provider_error('APIConnectionError'), 'provider_connection_failed', 'connection'),
    (RuntimeError(PRIVATE), 'provider_error', 'unexpected_error'),
])
def test_provider_errors_have_safe_actionable_types(configured, monkeypatch, caplog, exception, code, kind):
    sdk(monkeypatch, error=exception)
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == code
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['stage'] == 'generation' and diagnostic['failure_kind'] == kind
    assert diagnostic['provider_called'] is True and diagnostic['response_id'] is None
    if getattr(exception, 'status_code', None):
        assert diagnostic['http_status'] == exception.status_code
    assert_private_absent(metadata, caplog)


def test_rejected_schema_request_reports_allowed_parameter_without_provider_message(configured, monkeypatch, caplog):
    sdk(monkeypatch, error=provider_error('BadRequestError', status=400, code='invalid_json_schema', param='text.format.schema'))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == 'provider_request_rejected'
    diagnostic = metadata['provider_diagnostics']
    assert diagnostic['provider_error_code'] == 'invalid_json_schema'
    assert diagnostic['provider_error_param'] == 'text.format.schema'
    assert diagnostic['provider_request_id'] == 'req_test_failure'
    assert_private_absent(metadata, caplog)


def test_untrusted_error_fields_are_not_copied_into_diagnostics(configured, monkeypatch, caplog):
    sdk(monkeypatch, error=provider_error('BadRequestError', status=400, code={'secret': PRIVATE}, param={'secret': PRIVATE}))
    plan, metadata = generate(configured)
    assert plan is None and metadata['error_code'] == 'provider_request_rejected'
    assert 'provider_error_code' not in metadata['provider_diagnostics']
    assert 'provider_error_param' not in metadata['provider_diagnostics']
    assert_private_absent(metadata, caplog)


def test_inference_does_not_require_separate_model_metadata_permission(configured, monkeypatch):
    calls = sdk(monkeypatch)
    plan, metadata = generate(configured)
    assert plan is not None and metadata['provider'] == 'openai'
    assert calls['metadata_lookups'] == 0 and len(calls['requests']) == 1


def test_connection_probe_uses_the_actual_structured_chat_contract(configured, monkeypatch):
    calls = sdk(monkeypatch)
    with configured.factory() as session:
        result = ai.check_openai(session)
    assert result['connected'] and result['provider_called']
    assert result['probe_kind'] == 'structured_service_plan'
    request = calls['requests'][0]
    assert request['text']['format']['schema'] == ActionPlan.model_json_schema()
    assert request['max_output_tokens'] == settings.max_ai_output_tokens
    assert json.loads(request['input'])['message'] == 'Здравствуйте'
    assert calls['metadata_lookups'] == 0


def test_unstructured_ok_is_not_a_successful_chat_probe(configured, monkeypatch):
    sdk(monkeypatch, result_for('OK'))
    with configured.factory() as session:
        result = ai.check_openai(session)
    assert not result['connected'] and result['error_code'] == 'response_json_invalid'
    assert result['provider_called'] is True


def test_cleanup_failure_cannot_replace_completed_response_or_expose_exception(configured, monkeypatch, caplog):
    sdk(monkeypatch, cleanup_error=True)
    plan, metadata = generate(configured)
    assert plan is not None and metadata['provider_diagnostics']['stage'] == 'completed'
    assert_private_absent(metadata, caplog)
