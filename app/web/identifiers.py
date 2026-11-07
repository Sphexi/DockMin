"""Bounded integer parameter types for routes whose ids index SQLite INTEGER columns."""

from typing import Annotated

from fastapi import Path, Query
from pydantic import Field

MAX_ID = 2**63 - 1

PathId = Annotated[int, Path(ge=1, le=MAX_ID)]
QueryId = Annotated[int | None, Query(ge=1, le=MAX_ID)]
BoundedId = Annotated[int, Field(ge=1, le=MAX_ID)]
