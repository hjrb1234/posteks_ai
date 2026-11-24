"""Upgrade existing cases and solutions without touching the working database."""
import os
from pathlib import Path
import subprocess
import sys


def test_baseline_to_mail_head_preserves_records_and_adds_limitations(tmp_path):
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(('MAIL_', 'IMAP_', 'OPENAI_', 'POSTEKS_OPENAI_', 'SMTP_')):
            environment.pop(name)
    environment.update(DATABASE_URL='sqlite:///' + str(tmp_path / 'upgrade-test.sqlite'),
        STORAGE_DIR=str(tmp_path / 'private-storage'), APP_MODE='TEST', COOKIE_SECURE='false',
        OPENAI_API_KEY='', POSTEKS_OPENAI_API_KEY='')
    code = '''
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from server.db import Base, SessionLocal, engine
from server.workflows import ServiceCase, VerifiedSolution
config = Config('alembic.ini')
command.upgrade(config, '4336a05fa741')
with SessionLocal() as session:
    session.execute(text("""INSERT INTO service_cases
        (id,title,company,contact,email,equipment_model,serial_number,status,priority,source,created_at,updated_at)
        VALUES(1,'Migration sentinel','TEST COMPANY','','','','','new','normal','TEST','2026-10-19 00:00:00','2026-10-19 00:00:00')"""))
    case_id = 1
    session.execute(text("""INSERT INTO verified_solutions
        (id,title,equipment_model,symptoms,actions,source,customer_confirmed,status,visibility,verified_by,created_at,updated_at)
        VALUES(1,'Sentinel solution','PE38-12','Test symptom','Original action','Test source',1,'VERIFIED','internal','test-operator','2026-10-19 00:00:00','2026-10-19 00:00:00')"""))
    session.commit()
command.upgrade(config, 'head')
command.upgrade(config, 'head')
with SessionLocal() as session:
    assert session.get(ServiceCase,case_id).title == 'Migration sentinel'
    assert session.get(ServiceCase,case_id).company == 'TEST COMPANY'
    solution = session.get(VerifiedSolution,1)
    assert solution.actions == 'Original action' and solution.status == 'VERIFIED'
    assert solution.visibility == 'internal' and solution.limitations == ''
    assert session.execute(text('PRAGMA integrity_check')).scalar() == 'ok'
    assert session.execute(text('SELECT version_num FROM alembic_version')).scalar() == 'f3b5c7d9e012'
schema = inspect(engine)
for name,table in Base.metadata.tables.items():
    assert {column.name for column in table.columns} == {column['name'] for column in schema.get_columns(name)}, name
print('Baseline-to-head and repeated upgrade preserved existing records and match model columns')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'preserved existing records' in result.stdout


def test_mail_correspondence_upgrade_reclassifies_only_untouched_auto_cases(tmp_path):
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(('MAIL_', 'IMAP_', 'OPENAI_', 'POSTEKS_OPENAI_', 'SMTP_')):
            environment.pop(name)
    environment.update(DATABASE_URL='sqlite:///' + str(tmp_path / 'correspondence.sqlite'),
        STORAGE_DIR=str(tmp_path / 'private-storage'), APP_MODE='TEST', COOKIE_SECURE='false',
        OPENAI_API_KEY='', POSTEKS_OPENAI_API_KEY='')
    code = '''
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from server.db import SessionLocal
from server.workflows import ServiceCase, InboxMessage, CaseMessage, InternalTask
config = Config('alembic.ini')
command.upgrade(config, 'd8a1f60b3e52')
with SessionLocal() as session:
    for case_id in range(1, 9):
        session.execute(text("""INSERT INTO service_cases
            (id,title,company,contact,email,equipment_model,serial_number,status,priority,source,created_at,updated_at)
            VALUES(:id,:title,'','','','','','new','normal','IMAP_NEW','2026-10-19 00:00:00','2026-10-19 00:00:00')"""),
            {'id':case_id,'title':'Preserved case '+str(case_id)})
        session.execute(text("""INSERT INTO inbox_messages
            (id,message_id,content_hash,case_id,subject,sender,sender_name,recipients,body,"references",attachments,raw_mime,source,created_at)
            VALUES(:id,:message_id,:hash,:id,'Original subject','engineer@tests.invalid','','[]','Original body','[]','[]',X'54657374','IMAP_NEW','2026-10-19 00:00:00')"""),
            {'id':case_id,'message_id':'<migration-'+str(case_id)+'@tests.invalid>','hash':str(case_id).zfill(64)})
        session.execute(text("""INSERT INTO case_messages (case_id,body,kind,author,created_at)
            VALUES(:id,'Original body','client','engineer@tests.invalid','2026-10-19 00:00:00')"""), {'id':case_id})
    session.execute(text("UPDATE service_cases SET service_number='TEST-SO-1' WHERE id=2"))
    session.execute(text("UPDATE service_cases SET company='Edited by operator' WHERE id=3"))
    session.execute(text("""INSERT INTO case_messages (case_id,body,kind,author,created_at)
        VALUES(4,'Keep operator note','internal','operator','2026-10-19 01:00:00')"""))
    session.execute(text("""INSERT INTO internal_tasks (case_id,title,department,assignee,status,created_at,updated_at)
        VALUES(5,'Preserve task','Test','','open','2026-10-19 01:00:00','2026-10-19 01:00:00')"""))
    session.execute(text("UPDATE service_cases SET status='in_progress' WHERE id=6"))
    session.execute(text("UPDATE service_cases SET source='CHAT' WHERE id=7"))
    session.execute(text("UPDATE service_cases SET priority='high' WHERE id=8"))
    before={table:session.execute(text('SELECT * FROM '+table+' ORDER BY id')).mappings().all()
        for table in ('service_cases','inbox_messages','case_messages','internal_tasks')}
    session.commit()
command.upgrade(config, 'head')
command.upgrade(config, 'head')
with SessionLocal() as session:
    assert session.get(ServiceCase,1).kind == 'correspondence'
    for case_id in range(2,9):
        assert session.get(ServiceCase,case_id).kind == 'service', case_id
    assert session.get(ServiceCase,2).service_number == 'TEST-SO-1'
    for table,rows in before.items():
        after=session.execute(text('SELECT * FROM '+table+' ORDER BY id')).mappings().all()
        # Every preexisting column and row survives byte-for-byte.
        assert [{key:row[key] for key in rows[0]} for row in after] == rows, table
    assert session.get(InboxMessage,1).local_read_at is None
    assert session.execute(text('PRAGMA integrity_check')).scalar() == 'ok'
print('Only untouched auto-mail reclassified; notes, tasks and every original field preserved')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'every original field preserved' in result.stdout
