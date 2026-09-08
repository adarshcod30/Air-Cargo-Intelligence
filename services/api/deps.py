"""Shared API dependencies."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy.orm import Session

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.warehouse.loader import get_engine

log = get_logger(__name__)

_engine = None


def engine():
    """The serving connection, read-only where one is configured.

    The semantic layer already restricts which relations a query may
    name, but that is defence inside the application. This is the second
    layer: a role with SELECT on the five allowlisted views and no rights
    at all on the tables beneath them, so a query that ever escaped the
    compiler still could not write, and could not read an unaudited row.

    See db/readonly_role.sql. The fallback keeps a fresh checkout working
    and says so, rather than quietly serving as the schema owner.
    """
    global _engine
    if _engine is None:
        url = SETTINGS.database_url_readonly
        if url:
            _engine = get_engine(url)
        else:
            log.warning(
                "DATABASE_URL_READONLY is not set; serving as the owning role. "
                "Apply db/readonly_role.sql and set it before deploying."
            )
            _engine = get_engine(os.getenv("DATABASE_URL"))
    return _engine


def get_session() -> Iterator[Session]:
    with Session(engine()) as session:
        yield session
