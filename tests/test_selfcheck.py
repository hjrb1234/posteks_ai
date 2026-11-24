"""The end-user checker runs real local routes and never fakes a live result."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text, select, func
from sqlalchemy.exc import OperationalError

from test_core import env
from scripts import selfcheck
from server.config import settings
from server.models import Document
from server.workflows import ServiceCase


def rows_and_contexts():
    rows=[{'input':'Вопрос '+str(i), 'response':'Разный ответ '+str(i), 'engine':'openai',
           'provider_response_id':'resp_TEST_'+str(i), 'conversation_id':'one'} for i in range(3)]
    contexts=[{'current_message':r['input'],'history_items':i*2} for i,r in enumerate(rows)]
    return rows, contexts


def test_assessment_requires_current_message_history_and_one_conversation():
    rows, contexts=rows_and_contexts()
    assert selfcheck.evaluate_turns('basic',rows,contexts)[0]['status']=='pass'
    contexts[1]['current_message']='STALE OLD MESSAGE'
    assert selfcheck.evaluate_turns('basic',rows,contexts)[0]['status']=='fail'
    rows, contexts=rows_and_contexts();rows[-1]['conversation_id']='other'
    assert selfcheck.evaluate_turns('basic',rows,contexts)[0]['status']=='fail'


def test_http_200_without_model_response_is_not_a_pass():
    rows, contexts=rows_and_contexts()
    rows[0].update(engine='unavailable', provider_response_id=None,
        error_detail={'code':'response_json_invalid','message':'Ответ не соответствует JSON.'},
        provider_diagnostics={'stage':'parse','failure_kind':'response_json_invalid'})
    record=selfcheck.evaluate_turns('basic',rows,contexts)[0]
    assert record['status']=='fail' and record['evidence']['stage']=='parse'


def test_literal_long_repeat_is_reported_separately_from_successful_transport():
    rows, contexts=rows_and_contexts()
    rows[0]['response']=rows[1]['response']='Повтор ответа без изменения смысла. '*8
    records=selfcheck.evaluate_turns('basic',rows,contexts)
    assert records[0]['status']=='pass' and records[1]['status']=='warn'


def test_working_database_is_really_read_only(tmp_path,monkeypatch):
    database=tmp_path/'working.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('create table sentinel(value text)')
        connection.execute("insert into sentinel values ('PRESERVE')")
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr(settings,'database_url','sqlite:///'+str(database))
    with selfcheck.working_database() as session:
        assert session.execute(text('select value from sentinel')).scalar()=='PRESERVE'
        with pytest.raises(OperationalError):
            session.execute(text("insert into sentinel values ('FORBIDDEN')"))
        session.rollback()
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


def test_offline_route_checks_use_disposable_data_and_never_call_provider(env,monkeypatch):
    import openai
    def forbidden(*args,**kwargs):
        pytest.fail('Offline checker must not construct OpenAI')
    monkeypatch.setattr(openai,'OpenAI',forbidden)
    checks,private,recorder=selfcheck.exercise_application(
        {'main':'TEST','fast':'TEST','vision':'TEST','audio':'TEST','embedding':'TEST'},None,live=False)
    by_id={r['id']:r for r in checks}
    assert by_id['files.upload_route']['status']=='pass'
    assert by_id['files.access']['status']=='pass'
    assert by_id['negative.invalid_pdf']['status']=='pass'
    assert by_id['negative.blank_message']['status']=='pass'
    assert by_id['archive.no_actions']['status']=='pass'
    assert by_id['archive.deduplication']['status']=='pass'
    assert by_id['knowledge.actual_search']['status']=='pass'
    assert by_id['model.live']['status']=='skipped' and recorder.requests==[]
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Document))==0
        assert session.scalar(select(func.count()).select_from(ServiceCase))==0


def test_end_to_end_offline_report_keeps_keys_out_and_explicitly_skips_live(env,tmp_path,monkeypatch):
    import openai
    monkeypatch.setattr(settings,'openai_api_key','DONT_SHARE_THE_SECRET_VALUE')
    monkeypatch.setattr(openai,'OpenAI',lambda **kw: pytest.fail('No live flag'))
    @contextmanager
    def read_db():
        with env.factory() as session:
            yield session
    monkeypatch.setattr(selfcheck,'working_database',read_db)
    output=tmp_path/'reports'
    code=selfcheck.main(['--output',str(output)])
    assert code in {1,2}  # An empty working knowledge base is not called ready.
    html=__import__('pathlib').Path((output/'LATEST_REPORT.txt').read_text().strip())
    report=json.loads((html.parent/'support_report.json').read_text())
    assert 'DONT_SHARE_THE_SECRET_VALUE' not in json.dumps(report)
    assert next(r for r in report['checks'] if r['id']=='model.live')['status']=='skipped'
    assert report['metadata']['provider_calls_started']==0


def test_output_token_limit_is_configurable_and_bounded(monkeypatch):
    from server.config import Settings
    monkeypatch.setenv('MAX_AI_OUTPUT_TOKENS','8192')
    assert Settings().max_ai_output_tokens==8192
    monkeypatch.setenv('MAX_AI_OUTPUT_TOKENS','1')
    with pytest.raises(ValueError, match='MAX_AI_OUTPUT_TOKENS'):
        Settings()


def install_sdk_stub(monkeypatch, *, incomplete_direct=False, invalid_application=False):
    """Exercise the live orchestration branch without calling a real provider.

    The stub exists only in this test. Returned text is deliberately labelled
    TEST; this proves request plumbing and safe failure reporting, not quality
    or availability of any real language model.
    """
    import openai
    from server.orchestrator import local_plan
    requests = []

    class StubOpenAI:
        def __init__(self, **kwargs):
            self.responses = self

        def create(self, **kwargs):
            requests.append(kwargs)
            direct = isinstance(kwargs['input'], list)
            if direct:
                output = 'TEST direct reply ' + str(len(requests))
            elif invalid_application:
                output = '{"BROKEN_TEST_JSON":'
            else:
                context = json.loads(kwargs['input'])
                plan = local_plan(context)
                plan.update(action='answer', response_kind='conversation', technical_claims=[],
                            response='TEST application reply ' + str(len(requests)))
                if 'Укажи источник' in context['message'] and context['sources']:
                    quote = context['sources'][0]['text']
                    plan.update(response_kind='technical', response='TEST: ' + quote + ' [1]',
                        technical_claims=[{'text': quote, 'quote': quote, 'source_index': 1}])
                output = json.dumps(plan, ensure_ascii=False)
            return SimpleNamespace(id='resp_TEST_SELF_CHECK_' + str(len(requests)),
                _request_id='req_TEST_SELF_CHECK_' + str(len(requests)),
                status='incomplete' if direct and incomplete_direct else 'completed',
                incomplete_details=SimpleNamespace(reason='max_output_tokens'), output=[], output_text=output,
                usage=SimpleNamespace(input_tokens=10, output_tokens=20))

        def close(self):
            pass

    monkeypatch.setattr(settings, 'openai_api_key', 'TEST_NOT_A_REAL_KEY')
    monkeypatch.setattr(openai, 'OpenAI', StubOpenAI)
    return requests


def test_live_branch_via_sdk_stub_exercises_all_routes_and_context_without_network(env, monkeypatch):
    requests = install_sdk_stub(monkeypatch)
    checks, private, recorder = selfcheck.exercise_application(
        {'main':'TEST','fast':'TEST','vision':'TEST','audio':'TEST','embedding':'TEST'}, None, live=True)
    by_id = {row['id']: row for row in checks}
    for identifier in ('model.direct', 'model.basic', 'model.technical', 'model.memory',
                       'model.grounded', 'model.customer_symptom'):
        assert by_id[identifier]['status'] == 'pass', (identifier, by_id[identifier])
    assert len(requests) == 15
    assert sum(r.get('provider_call_started') is True for r in recorder.requests) == 15
    assert all(request['store'] is False for request in requests)
    app_contexts = [json.loads(request['input']) for request in requests if isinstance(request['input'], str)]
    assert [len(context['recent_history']) for context in app_contexts[:5]] == [0, 2, 4, 6, 8]
    assert [row['input'] for row in private['basic']] == [context['message'] for context in app_contexts[:5]]
    for context in app_contexts[6:8]:
        assert context['state']['known_facts']['equipment_model'] == 'PE38-12'
        assert private['technical'][0]['input'] in context['state']['attempted_actions']
    assert private['technical'][-1]['state']['known_facts']['equipment_model'] == 'PE38-12'
    assert private['technical'][0]['input'] in private['technical_memory']['attempted_actions']
    assert by_id['model.memory']['evidence']['attempted_checks_count'] >= 1
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(ServiceCase)) == 0


def test_sdk_stub_rejected_application_answer_stops_paid_followup_tests(env, monkeypatch):
    requests = install_sdk_stub(monkeypatch, invalid_application=True)
    checks, private, recorder = selfcheck.exercise_application(
        {'main':'TEST','fast':'TEST','vision':'TEST','audio':'TEST','embedding':'TEST'}, None, live=True)
    by_id = {row['id']: row for row in checks}
    assert by_id['model.direct']['status'] == 'pass'
    assert by_id['model.basic']['status'] == 'fail'
    assert by_id['model.basic']['evidence']['failure_kind'] == 'response_json_invalid'
    assert by_id['model.basic']['evidence']['expected_turns'] == 5
    assert by_id['model.technical']['status'] == 'skipped'
    assert len(requests) == 6 and len(private['basic']) == 1
    assert 'model.grounded' not in by_id and 'model.customer_symptom' not in by_id


def test_direct_sdk_stub_incomplete_response_is_failure_even_with_nonempty_text(env, monkeypatch):
    from scripts.dialogue_diagnostics import ProviderRecorder, run_direct
    requests = install_sdk_stub(monkeypatch, incomplete_direct=True)
    recorder = ProviderRecorder(live=True)
    report = run_direct([('basic', ['Здравствуйте', 'Уточнение'])], recorder, model='TEST', configured=True)
    row = report['scenarios'][0]['turns'][0]
    assert row['status'] == 'failed' and row['response'] is None
    assert row['error_code'] == 'response_output_limit'
    assert row['provider_status'] == 'incomplete'
    assert row['provider_response_id'] == 'resp_TEST_SELF_CHECK_1'
    assert row['usage'] == {'input_tokens': 10, 'output_tokens': 20}
    assert len(requests) == 1


def test_diagnostic_wrapper_disables_hidden_sdk_retries_only_for_its_constructor():
    from scripts.dialogue_diagnostics import ProviderRecorder
    constructed = []
    def constructor(**kwargs):
        constructed.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace())
    wrapped = ProviderRecorder(live=True).wrap_constructor(constructor)
    wrapped(api_key='TEST_NOT_REAL', max_retries=1)
    assert constructed == [{'api_key':'TEST_NOT_REAL', 'max_retries':0}]
