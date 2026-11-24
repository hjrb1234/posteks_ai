"""Withdrawn sources must not leak through a request's cached ORM objects."""
from sqlalchemy import select

from test_core import env, login, upload
from server import main, workflows
from server.models import Document


def test_publication_revoked_during_generation_is_checked_fresh(env, monkeypatch):
    login(env.client)
    document_id = upload(env.client, visibility='public').json()[0]['id']
    held = []
    def delayed_answer(session, *args, **kwargs):
        held.append(session.get(Document, document_id))
        assert held[0].visibility == 'public'
        with env.factory() as other:
            other.get(Document, document_id).visibility = 'internal'
            other.commit()
        return {'content': 'PRIVATE_QUOTATION_MUST_NOT_ESCAPE',
                'sources': [{'document_id': document_id, 'page': 1, 'text': 'PRIVATE_EXCERPT'}],
                'engine': 'injected', 'action': 'answer'}
    monkeypatch.setattr(main.ai, 'answer', delayed_answer)
    reply = env.client.post('/api/chat', json={'message': 'PE38-12 регулировка'}).json()
    assert 'PRIVATE_' not in str(reply) and reply['message']['sources'] == []
    history = env.client.get('/api/conversations/' + reply['conversation_id']).json()
    assert 'PRIVATE_' not in str(history)
    env.client.post('/api/auth/logout')
    assert env.client.get(f'/api/documents/{document_id}/file').status_code == 404


def test_verified_solution_revoked_during_generation_is_checked_fresh(env, monkeypatch):
    with env.factory() as session:
        solution = workflows.VerifiedSolution(title='Решение', equipment_model='PE38-12', symptoms='Подача',
            actions='Подтверждённое действие', status='VERIFIED', visibility='public', source='Тест', customer_confirmed=True)
        session.add(solution)
        session.commit()
        solution_id = solution.id
    held = []
    def delayed_answer(session, *args, **kwargs):
        held.append(session.get(workflows.VerifiedSolution, solution_id))
        assert held[0].visibility == 'public'
        with env.factory() as other:
            other.get(workflows.VerifiedSolution, solution_id).status = 'CANDIDATE'
            other.commit()
        return {'content': 'PRIVATE_SOLUTION_MUST_NOT_ESCAPE',
                'sources': [{'solution_id': solution_id, 'text': 'PRIVATE_EXCERPT'}],
                'engine': 'injected', 'action': 'answer'}
    monkeypatch.setattr(main.ai, 'answer', delayed_answer)
    reply = env.client.post('/api/chat', json={'message': 'PE38-12 подача'}).json()
    assert 'PRIVATE_' not in str(reply) and reply['message']['sources'] == []
    assert env.client.get(f'/api/knowledge/solutions/{solution_id}').status_code == 404
