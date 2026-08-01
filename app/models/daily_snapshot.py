"""DailySnapshot model — a per-user, per-day aggregate of commit activity."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_daily_snapshots_user_id_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date: Mapped[datetime.date] = mapped_column()
    commit_count: Mapped[int] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="daily_snapshots")

    def __repr__(self) -> str:
        return (
            f"<DailySnapshot id={self.id} user_id={self.user_id} "
            f"date={self.date} commit_count={self.commit_count}>"
        )
