"""Persistent conversation state, specialist handoff, and document diagnostics.

Revision ID: b4f2a13c90de
Revises: 4336a05fa741
"""
from alembic import op
import sqlalchemy as sa
revision = 'b4f2a13c90de'
down_revision = '4336a05fa741'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('indexing_details', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('document_pages', sa.Column('layout', sa.JSON(), server_default='{}', nullable=False))
    op.create_table('conversation_states',
        sa.Column('conversation_id', sa.String(36), sa.ForeignKey('conversations.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('state', sa.JSON(), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_table('escalations', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.String(36), sa.ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('service_cases.id'), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('internal_tasks.id'), nullable=False),
        sa.Column('status', sa.String(30), nullable=False), sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False), sa.Column('known_facts', sa.JSON(), nullable=False),
        sa.Column('missing_information', sa.JSON(), nullable=False), sa.Column('attempted_actions', sa.JSON(), nullable=False),
        sa.Column('document_sources', sa.JSON(), nullable=False), sa.Column('attachments', sa.JSON(), nullable=False),
        sa.Column('requested_action', sa.String(40), nullable=False), sa.Column('department', sa.String(160), nullable=False),
        sa.Column('priority', sa.String(16), nullable=False), sa.Column('assignee', sa.String(160), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_index('ix_escalations_conversation_id', 'escalations', ['conversation_id'], unique=True)
    for column in ('case_id', 'task_id', 'status'):
        op.create_index('ix_escalations_' + column, 'escalations', [column])


def downgrade():
    op.drop_table('escalations')
    op.drop_table('conversation_states')
    with op.batch_alter_table('document_pages') as batch:
        batch.drop_column('layout')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('indexing_details')
