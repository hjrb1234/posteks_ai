"""Registry identity is available to staff, without becoming a public repair rule."""
from sqlalchemy import select
from test_core import env, login
from server.equipment_context import lookup
from server.orchestrator import _retrieve, initial_state
from server.workflows import Equipment, normalize_serial


def seed(env):
    with env.factory() as session:
        for serial, model in [('AA-123', 'PE38-12'), ('BB-456', 'PE38-17')]:
            session.add(Equipment(serial_number=serial, normalized_serial=normalize_serial(serial),
                model=model, customer='ООО «Тестовый заказчик»', project='TEST', name='Формирователь'))
        session.commit()
        return list(session.scalars(select(Equipment.id).order_by(Equipment.id)))


def test_registry_lookup_exact_serial_and_company_not_phone_or_substring(env):
    ids = seed(env)
    with env.factory() as session:
        assert [r.id for r in lookup(session, 'AA 123')] == ids[:1]
        assert [r.id for r in lookup(session, 'серийный номер AA-123, нужна инструкция')] == ids[:1]
        assert [r.id for r in lookup(session, 'ооо «тестовый заказчик»')] == ids
        assert [r.id for r in lookup(session, 'ООО «Тестовый заказчик»', model='РЕ38-17')] == ids[1:]
        for query in ['123', '+7 912 312 31 23', 'Тестовый', 'AA-1234', 'PE38-12']:
            assert lookup(session, query) == [], query


def test_registry_evidence_requires_staff_and_guarantees_no_warranty(env):
    ids = seed(env)
    assert env.client.get('/api/equipment/lookup', params={'q': 'AA-123'}).status_code == 401
    assert env.client.get(f'/api/equipment/{ids[0]}/evidence').status_code == 401
    login(env.client)
    result = env.client.get('/api/equipment/lookup', params={'q': 'AA 123'}).json()
    assert len(result) == 1 and result[0]['id'] == ids[0]
    evidence = env.client.get(f'/api/equipment/{ids[0]}/evidence').json()
    assert evidence['warranty_status'] == 'unknown'
    assert 'не совместимость деталей' in evidence['text']
    assert env.client.get('/api/equipment/99999/evidence').status_code == 404


def test_registry_retrieval_staff_only_and_multiple_models_do_not_conflict(env):
    seed(env)
    with env.factory() as session:
        query = 'ООО «Тестовый заказчик»'
        public, machine, conflict = _retrieve(session, query, initial_state(), None, None, False)
        assert public == [] and not conflict
        staff, machine, conflict = _retrieve(session, query, initial_state(), None, None, True)
        assert len(staff) == 2 and not conflict
        assert all(r['source_type'] == 'equipment_registry' and r['models'] == [] for r in staff)
        assert len({r['equipment_id'] for r in staff}) == 2
    # Even a logged-in staff user in the CLIENT chat gets no private registry.
    login(env.client)
    result = env.client.post('/api/chat', json={'message': 'серийный номер AA-123'}).json()
    assert not result['message']['sources']
    assert 'Тестовый заказчик' not in result['message']['content']
