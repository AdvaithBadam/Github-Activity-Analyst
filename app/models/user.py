"""User model — represents a tracked GitHub account."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.daily_snapshot import DailySnapshot
    from app.models.repo import Repo


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_username: Mapped[str] = mapped_column(unique=True, index=True)
    github_created_at: Mapped[datetime.datetime] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────
    repos: Mapped[list[Repo]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    daily_snapshots: Mapped[list[DailySnapshot]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} github_username={self.github_username!r}>"
