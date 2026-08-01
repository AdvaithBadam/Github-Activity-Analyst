"""Shared declarative base for all SQLAlchemy ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class that all ORM models inherit from.

    Import this in each model file and in Alembic's env.py so that
    Base.metadata knows about every table.
    """

    pass
