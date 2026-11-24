"""Auditable candidates from mail threads and explicit solution limitations.

Revision ID: d8a1f60b3e52
Revises: c7d9e21a640f
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8a1f60b3e52'
down_revision = 'c7d9e21a640f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('verified_solutions', sa.Column('limitations', sa.Text(), nullable=False, server_default=''))
    op.create_table('mail_solution_origins',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('origin_key', sa.String(64), nullable=False, unique=True),
        sa.Column('solution_id', sa.Integer(), sa.ForeignKey('verified_solutions.id'), nullable=False, unique=True),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('service_cases.id'), nullable=False),
        sa.Column('inbox_id', sa.Integer(), sa.ForeignKey('inbox_messages.id'), nullable=False),
        sa.Column('equipment_model', sa.String(120), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.String(160), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table('mail_solution_origins')
    with op.batch_alter_table('verified_solutions') as batch:
        batch.drop_column('limitations')
