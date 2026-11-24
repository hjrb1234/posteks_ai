"""Separate answer permission from access to original document files."""
from alembic import op
import sqlalchemy as sa

revision = 'f3b5c7d9e012'
down_revision = 'e2a4b6c8d010'
branch_labels = None
depends_on = None


def upgrade():
    # Existing documents remain private unless already explicitly published.
    op.add_column('documents', sa.Column('answer_access', sa.String(24), nullable=False,
                                        server_default='staff_only'))


def downgrade():
    op.drop_column('documents', 'answer_access')
