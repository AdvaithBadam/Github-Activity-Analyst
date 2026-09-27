"""Repo model — a GitHub repository belonging to a tracked User."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.commit import Commit
    from app.models.user import User


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("user_id", "github_repo_id", name="uq_repos_user_id_github_repo_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    github_repo_id: Mapped[int] = mapped_column(index=True)
    name: Mapped[str] = mapped_column()
    is_owner: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    owner_login: Mapped[str | None] = mapped_column(nullable=True)
    github_created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="repos")
    commits: Mapped[list[Commit]] = relationship(
        back_populates="repo",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Repo id={self.id} name={self.name!r} user_id={self.user_id}>"
