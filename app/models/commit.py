"""Commit model — an individual commit within a Repo.

NOTE: There is deliberately no user_id FK on this table. The owning user is
derivable via repo.user_id, and the column was intentionally excluded to
avoid data redundancy.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.repo import Repo


class Commit(Base):
    __tablename__ = "commits"
    __table_args__ = (
        UniqueConstraint("repo_id", "sha", name="uq_commits_repo_id_sha"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    sha: Mapped[str] = mapped_column(index=True)
    message: Mapped[str] = mapped_column()
    committed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────
    repo: Mapped[Repo] = relationship(back_populates="commits")

    def __repr__(self) -> str:
        return f"<Commit id={self.id} sha={self.sha[:8]!r} repo_id={self.repo_id}>"
