"""Re-export all models so that ``import app.models`` registers every table
with ``Base.metadata`` (important for Alembic autogenerate).

Usage::

    from app.models import Base, User, Repo, Commit, DailySnapshot
"""

from app.models.base import Base
from app.models.commit import Commit
from app.models.daily_snapshot import DailySnapshot
from app.models.repo import Repo
from app.models.user import User

__all__ = [
    "Base",
    "Commit",
    "DailySnapshot",
    "Repo",
    "User",
]
