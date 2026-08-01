"""Repo model — a GitHub repository belonging to a tracked User."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.commit import Commit
    from app.models.user import User


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column()
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
