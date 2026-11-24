"""Durable read-only IMAP jobs, UID epochs, and private attachment metadata.

Revision ID: c7d9e21a640f
Revises: b4f2a13c90de
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7d9e21a640f'
down_revision = 'b4f2a13c90de'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('mail_folder_states',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_key', sa.String(64), nullable=False),
        sa.Column('folder', sa.String(512), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('uidvalidity', sa.String(80)),
        sa.Column('last_uid', sa.BigInteger(), nullable=False),
        sa.Column('new_initialized', sa.Boolean(), nullable=False),
        sa.Column('initial_upper_uid', sa.BigInteger()),
        sa.Column('initial_since', sa.String(10)),
        sa.Column('history_last_uid', sa.BigInteger(), nullable=False),
        sa.Column('history_since', sa.String(10)),
        sa.Column('history_days', sa.Integer()),
        sa.Column('history_completed', sa.Boolean(), nullable=False),
        sa.Column('last_sync', sa.DateTime()),
        sa.Column('error', sa.Text()),
        sa.UniqueConstraint('account_key', 'folder', name='uq_mail_folder_account'))
    op.create_index('ix_mail_folder_states_account_key', 'mail_folder_states', ['account_key'])
    op.create_table('mail_remote_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_key', sa.String(64), nullable=False),
        sa.Column('folder', sa.String(512), nullable=False),
        sa.Column('uidvalidity', sa.String(80), nullable=False),
        sa.Column('uid', sa.BigInteger(), nullable=False),
        sa.Column('inbox_id', sa.Integer(), sa.ForeignKey('inbox_messages.id')),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('direction', sa.String(16), nullable=False),
        sa.Column('error_code', sa.String(80)),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('account_key', 'folder', 'uidvalidity', 'uid', name='uq_mail_remote_uid'))
    op.create_index('ix_mail_remote_messages_account_key', 'mail_remote_messages', ['account_key'])
    op.create_index('ix_mail_remote_messages_inbox_id', 'mail_remote_messages', ['inbox_id'])
    op.create_table('mail_attachments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('inbox_id', sa.Integer(), sa.ForeignKey('inbox_messages.id'), nullable=False),
        sa.Column('filename', sa.String(240), nullable=False),
        sa.Column('content_type', sa.String(160), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('storage_path', sa.Text()),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('reason', sa.String(240)),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('inbox_id', 'sha256', 'filename', name='uq_mail_attachment'))
    op.create_index('ix_mail_attachments_inbox_id', 'mail_attachments', ['inbox_id'])
    op.create_table('mail_sync_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_key', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('batch_size', sa.Integer(), nullable=False),
        sa.Column('imported', sa.Integer(), nullable=False),
        sa.Column('duplicates', sa.Integer(), nullable=False),
        sa.Column('fetched', sa.Integer(), nullable=False),
        sa.Column('errors', sa.Integer(), nullable=False),
        sa.Column('progress', sa.JSON(), nullable=False),
        sa.Column('has_more', sa.Boolean(), nullable=False),
        sa.Column('stop_requested', sa.Boolean(), nullable=False),
        sa.Column('error_code', sa.String(80)),
        sa.Column('error', sa.Text()),
        sa.Column('actor', sa.String(160), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime()),
        sa.Column('finished_at', sa.DateTime()))
    op.create_index('ix_mail_sync_runs_account_key', 'mail_sync_runs', ['account_key'])
    op.create_index('ix_mail_sync_runs_status', 'mail_sync_runs', ['status'])
    op.create_table('mail_sync_controls',
        sa.Column('account_key', sa.String(64), primary_key=True),
        sa.Column('background_enabled', sa.Boolean(), nullable=False),
        sa.Column('poll_seconds', sa.Integer(), nullable=False),
        sa.Column('lease_owner', sa.String(80)),
        sa.Column('lease_until', sa.DateTime()),
        sa.Column('last_heartbeat', sa.DateTime()),
        sa.Column('last_connection', sa.DateTime()),
        sa.Column('connection_error', sa.String(80)),
        sa.Column('updated_at', sa.DateTime(), nullable=False))


def downgrade():
    # No automatic downgrade is run by application startup.
    op.drop_table('mail_sync_controls')
    op.drop_table('mail_sync_runs')
    op.drop_table('mail_attachments')
    op.drop_table('mail_remote_messages')
    op.drop_table('mail_folder_states')
