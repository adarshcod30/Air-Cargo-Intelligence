"""Shared API dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from services.warehouse.loader import get_engine

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def get_session() -> Iterator[Session]:
    with Session(engine()) as session:
        yield session
