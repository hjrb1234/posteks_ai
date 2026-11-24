"""A recent first poll is distinct from an explicit, resumable year import."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from test_mail_sync import mail_app, MockIMAPServer, make_eml, run_sync


def status(context):
    with context.factory() as session:
        return context.sync.get_mail_status(session)


def test_recent_default_does_not_claim_a_year_is_loaded(mail_app):
    before = status(mail_app)
    assert before['import_window']['initial_days'] == 7
    assert before['import_window']['recommended_history_days'] == 365
    assert before['history']['status'] == 'not_started' and not before['history']['completed']
    server = MockIMAPServer({1: make_eml('<old@tests.invalid>'), 2: make_eml('<new@tests.invalid>')})
    server.internal_dates[('INBOX', 1)] = (datetime.now(timezone.utc) - timedelta(days=200)).date()
    run_sync(mail_app, server, kind='new')
    after = status(mail_app)
    assert after['stored_messages'] == 1
    assert after['history']['status'] == 'not_started' and not after['history']['completed']
    assert server.fetched_uids() == [2]


def test_one_year_job_reports_all_batches_duplicates_and_persistent_coverage(mail_app):
    server = MockIMAPServer({uid: make_eml(f'<year-{uid}@tests.invalid>') for uid in [1, 2, 3]})
    server.internal_dates[('INBOX', 1)] = (datetime.now(timezone.utc) - timedelta(days=200)).date()
    run_sync(mail_app, server, kind='new')
    first = run_sync(mail_app, server, kind='history', days=365, batch_size=1)
    history = status(mail_app)['history']
    assert history['run_id'] == first['id'] and history['days'] == 365
    assert history['processed'] == 1 and history['eligible_in_run'] == 3
    assert history['remaining'] == 2 and history['has_more']
    assert history['mailbox_messages'] == 3 and not history['completed']
    run_sync(mail_app, server, kind='history', days=365, batch_size=1, run_id=first['id'])
    run_sync(mail_app, server, kind='history', days=365, batch_size=1, run_id=first['id'])
    final = status(mail_app)
    assert final['stored_messages'] == 3
    assert final['history']['processed'] == final['history']['eligible_in_run'] == 3
    assert final['history']['remaining'] == 0 and final['history']['completed']
    assert final['history']['imported'] == 1 and final['history']['duplicates'] == 2
    with mail_app.factory() as session:
        for _ in range(25):
            session.add(mail_app.models.MailSyncRun(account_key=mail_app.sync.account_key(), kind='new', status='completed'))
        session.commit()
    # A history job must not disappear when it falls outside the 20-row log.
    assert status(mail_app)['history']['run_id'] == first['id']
    assert status(mail_app)['history']['completed']


def test_history_progress_survives_mid_batch_network_failure_and_retry(mail_app):
    server = MockIMAPServer({uid: make_eml(f'<retry-year-{uid}@tests.invalid>') for uid in [1, 2, 3]})
    server.fail_once.add(('INBOX', 2))
    failed = run_sync(mail_app, server, kind='history', batch_size=100)
    assert failed['status'] == 'failed'
    progress = status(mail_app)['history']
    assert progress['processed'] == 1 and progress['remaining'] == 2
    completed = run_sync(mail_app, server, kind='history', batch_size=100, run_id=failed['id'])
    assert completed['status'] == 'completed' and completed['errors'] == 0
    history = status(mail_app)['history']
    assert history['completed'] and history['processed'] == 3
    assert history['folders'][0]['failed_attempts'] == 1 or history['folders'][1]['failed_attempts'] == 1


def test_scanned_history_with_skipped_mail_is_not_labelled_complete(mail_app):
    server = MockIMAPServer({1: b'oversized', 2: make_eml('<after-skip@tests.invalid>')})
    server.size_overrides[('INBOX', 1)] = 100 * 1024 * 1024
    run_sync(mail_app, server, kind='history')
    history = status(mail_app)['history']
    assert history['scanned_complete'] and not history['completed']
    assert history['status'] == 'partial' and history['errors'] == 1
    assert history['processed'] == history['eligible_in_run'] == 2
    run_sync(mail_app, server, kind='history')
    repeated = status(mail_app)['history']
    assert repeated['job_errors'] == 0 and repeated['unresolved_skips'] == 1
    assert not repeated['completed'] and repeated['status'] == 'partial'
    assert repeated['unresolved'][0]['uid'] == 1


def test_worker_drains_one_year_job_across_more_than_one_batch(mail_app, monkeypatch):
    server = MockIMAPServer({uid: make_eml(f'<worker-year-{uid}@tests.invalid>') for uid in [1, 2, 3]})
    monkeypatch.setattr(mail_app.sync.imaplib, 'IMAP4_SSL', server.factory)
    with mail_app.factory() as session:
        queued = mail_app.sync.queue_sync(session, kind='history', days=365, batch_size=1)
    class StopWhenFinished:
        stopped = False
        iterations = 0
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            self.iterations += 1
            with mail_app.factory() as session:
                run = session.get(mail_app.models.MailSyncRun, queued['id'])
                self.stopped = bool(run.status == 'completed' and not run.has_more)
            self.stopped = self.stopped or self.iterations >= 5
            return self.stopped
    signal = StopWhenFinished()
    mail_app.sync._worker_loop(signal)
    with mail_app.factory() as session:
        run = session.get(mail_app.models.MailSyncRun, queued['id'])
        assert run.status == 'completed' and run.imported == 3 and not run.has_more
        assert session.query(mail_app.models.MailSyncRun).count() == 1
    assert signal.iterations == 3 and server.fetched_uids() == [1, 2, 3]
    assert status(mail_app)['history']['completed']
