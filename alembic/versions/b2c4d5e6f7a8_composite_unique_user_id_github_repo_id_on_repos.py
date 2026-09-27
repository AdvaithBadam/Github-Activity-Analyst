"""composite unique user_id github_repo_id on repos

Revision ID: b2c4d5e6f7a8
Revises: afbba6070634
Create Date: 2026-09-27 13:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'afbba6070634'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('ix_repos_github_repo_id', table_name='repos')
    op.create_index(op.f('ix_repos_github_repo_id'), 'repos', ['github_repo_id'], unique=False)
    op.create_unique_constraint('uq_repos_user_id_github_repo_id', 'repos', ['user_id', 'github_repo_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_repos_user_id_github_repo_id', 'repos', type_='unique')
    op.drop_index(op.f('ix_repos_github_repo_id'), table_name='repos')
    op.create_index(op.f('ix_repos_github_repo_id'), 'repos', ['github_repo_id'], unique=True)
