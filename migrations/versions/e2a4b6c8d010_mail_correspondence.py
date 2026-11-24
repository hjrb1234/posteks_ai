"""Separate mail correspondence from explicitly created service cases."""
from alembic import op
import sqlalchemy as sa
revision = 'e2a4b6c8d010'
down_revision = 'd8a1f60b3e52'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('service_cases', sa.Column('kind', sa.String(24), nullable=False, server_default='service'))
    op.create_index('ix_service_cases_kind', 'service_cases', ['kind'])
    op.add_column('inbox_messages', sa.Column('local_read_at', sa.DateTime(), nullable=True))
    # Preserve every record. Only untouched auto-created mail containers are
    # reclassified; a numbered, assigned or operator-edited case stays a case.
    op.execute("""UPDATE service_cases SET kind='correspondence'
        WHERE source IN ('TEST_IMPORT','IMAP_NEW','IMAP_HISTORY','IMAP','IMAP_INBOX','IMAP_SENT')
        AND (service_number IS NULL OR service_number='')
        AND status='new' AND priority='normal' AND equipment_model='' AND serial_number='' AND company=''
        AND NOT EXISTS (SELECT 1 FROM internal_tasks WHERE internal_tasks.case_id=service_cases.id)
        AND NOT EXISTS (SELECT 1 FROM case_messages WHERE case_messages.case_id=service_cases.id
            AND author NOT IN (SELECT sender FROM inbox_messages WHERE inbox_messages.case_id=service_cases.id))""")


def downgrade():
    op.drop_column('inbox_messages','local_read_at')
    op.drop_index('ix_service_cases_kind',table_name='service_cases')
    op.drop_column('service_cases','kind')
