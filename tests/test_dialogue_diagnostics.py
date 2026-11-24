"""Diagnostic evidence cannot invent a completed live integration test."""
import json
from types import SimpleNamespace

import pytest

from scripts.dialogue_diagnostics import (BASIC_TURNS, ProviderRecorder, application_observations, comparison_complete,
    fingerprint, main, source_references, summarize_request)


def test_structured_request_summary_keeps_current_turn_and_history_without_private_excerpt():
    request = {'model': 'model-test', 'instructions': 'System instruction', 'store': False,
        'input': json.dumps({'message': 'Объясни предыдущий ответ проще',
            'recent_history': [{'role': 'user', 'content': 'Ранее спросили'},
                               {'role': 'assistant', 'content': 'Ранее ответили'}],
            'sources': [{'document_id': 7, 'page': 90, 'text': 'PRIVATE_MANUAL_EXCERPT'}]}, ensure_ascii=False)}
    result = summarize_request(request)
    assert result['current_message'] == 'Объясни предыдущий ответ проще'
    assert result['history_items'] == 2 and result['history_roles'] == ['user', 'assistant']
    assert result['source_references'] == [{'document_id': 7, 'page': 90}]
    assert result['model'] == 'model-test' and result['store'] is False
    assert 'PRIVATE_MANUAL_EXCERPT' not in json.dumps(result)
    assert 'private_request_payload' not in result
    assert result['input_sha256'] == fingerprint(request['input'])


def test_direct_message_summary_uses_newest_user_text_not_old_answer():
    result = summarize_request({'model': 'model-test', 'input': [
        {'role': 'user', 'content': BASIC_TURNS[0]},
        {'role': 'assistant', 'content': 'Старый ответ'},
        {'role': 'user', 'content': BASIC_TURNS[1]}]})
    assert result['current_message'] == BASIC_TURNS[1]
    assert result['history_roles'] == ['user', 'assistant']
    assert result['input_roles'] == ['user', 'assistant', 'user']
    assert result['history_items'] == 2


def test_opt_in_payload_capture_redacts_transport_secret_fields():
    result = summarize_request({'model': 'test', 'api_key': 'SECRET_VALUE',
        'input': {'message': 'Тест', 'authorization': 'AUTH_SECRET',
                  'nested': {'password': 'PASSWORD_SECRET'}}}, capture_content=True)
    assert result['private_request_payload']['api_key'] == '[REDACTED]'
    serialized = json.dumps(result)
    assert all(value not in serialized for value in ('SECRET_VALUE', 'AUTH_SECRET', 'PASSWORD_SECRET'))


def test_provider_wrapper_without_live_flag_never_calls_underlying_sdk():
    called = []
    inner = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: called.append(kwargs)))
    recorder = ProviderRecorder(live=False)
    client = recorder.wrap_constructor(lambda **kwargs: inner)(api_key='not-a-real-key')
    with pytest.raises(RuntimeError, match='--live'):
        client.responses.create(model='test', input='Здравствуйте')
    assert called == [] and recorder.requests[0]['status'] == 'blocked_without_live_flag'
    assert not recorder.requests[0].get('provider_call_started')


def test_provider_wrapper_enforces_bounded_generation_calls():
    called = []
    def generate(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(id='response-test', status='completed', usage=None, output_text='Тест')
    inner = SimpleNamespace(responses=SimpleNamespace(create=generate))
    recorder = ProviderRecorder(live=True, limit=1)
    client = recorder.wrap_constructor(lambda **kwargs: inner)()
    client.responses.create(model='test', input='Первый')
    with pytest.raises(RuntimeError, match='limit'):
        client.responses.create(model='test', input='Второй')
    assert len(called) == 1
    assert recorder.requests[0]['provider_response_id'] == 'response-test'
    assert recorder.requests[1]['status'] == 'blocked_call_limit'


def completed_report():
    return {'direct_model': {'status': 'executed', 'scenarios': [{'name': 'basic', 'turns': [
        {'input': value, 'response': 'DIRECT_TEST', 'status': 'returned', 'provider_response_id': 'direct-test-' + str(index)}
        for index, value in enumerate(BASIC_TURNS)]}]}, 'application': {'status': 'executed', 'scenarios': [{'name': 'basic', 'turns': [
        {'input': value, 'response': 'APP_TEST', 'http_status': 200, 'engine': 'openai',
         'provider_response_id': 'application-test-' + str(index), 'conversation_id': 'same-owned-chat'} for index, value in enumerate(BASIC_TURNS)]}]}}


def test_completed_comparison_requires_all_turns_real_provider_ids_and_one_thread():
    report = completed_report()
    assert comparison_complete(report)
    report['application']['scenarios'][0]['turns'][-1]['engine'] = 'search_only'
    assert not comparison_complete(report)
    report = completed_report()
    report['application']['scenarios'][0]['turns'][-1]['conversation_id'] = 'different-chat'
    assert not comparison_complete(report)
    report = completed_report()
    report['direct_model']['scenarios'][0]['turns'].pop()
    assert not comparison_complete(report)
    report = completed_report()
    report['application']['scenarios'][0]['turns'][-1]['provider_response_id'] = 'application-test-0'
    assert not comparison_complete(report)
    assert not comparison_complete({'direct_model': {'status': 'not_run'}, 'application': {'status': 'executed'}})


def test_nested_source_identity_deduplicated_without_copying_text():
    result = source_references({'sources': [{'document_id': 1, 'page': 4, 'text': 'PRIVATE'},
        {'document_id': 1, 'page': 4, 'text': 'PRIVATE'}, {'solution_id': 6, 'actions': 'PRIVATE'}]})
    assert result == [{'document_id': 1, 'page': 4}, {'solution_id': 6}]


def test_verbatim_repetitions_and_reused_response_ids_are_visible_as_observations():
    application = {'scenarios': [{'name': 'basic', 'turns': [
        {'response': 'Первый', 'provider_response_id': 'response-1'},
        {'response': 'Одинаковый', 'provider_response_id': 'response-2'},
        {'response': 'Одинаковый', 'provider_response_id': 'response-2'},
        {'response': None, 'provider_response_id': None}]}]}
    result = application_observations(application)[0]
    assert result['verbatim_repeated_response_turns'] == [[2, 3]]
    assert result['reused_provider_response_id_turns'] == [[2, 3]]


def test_offline_cli_uses_actual_application_routes_but_never_claims_live_provider(env, monkeypatch, tmp_path):
    from server import db
    monkeypatch.setattr(db, 'SessionLocal', env.factory)
    output = tmp_path / 'private-comparison.json'
    assert main(['--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['provider_calls_started'] == report['provider_calls_returned'] == 0
    assert report['direct_model']['status'] == 'not_run'
    assert report['direct_model']['responses_invented'] is False
    assert report['live_comparison_completed'] is False
    basic = report['application']['scenarios'][0]['turns']
    assert [row['input'] for row in basic] == list(BASIC_TURNS)
    assert len(report['application']['scenarios'][1]['turns']) == 3
    assert report['method'] == 'isolated_actual_fastapi_routes'


# The fixture lives in the repository's shared integration test module.
from test_core import env  # noqa: E402,F401
