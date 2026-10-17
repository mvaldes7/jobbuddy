"""Dialect-aware embedding column.

* postgresql -> pgvector ``vector(384)`` (HNSW cosine index created in migration 0001)
* mssql      -> Azure SQL ``VECTOR(384)``; bound as JSON text, which SQL Server casts implicitly
* other      -> TEXT holding a JSON array (SQLite dev/tests; cosine computed in Python)
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.types import Text, TypeDecorator, UserDefinedType

EMBEDDING_DIM = 384


class _MssqlVector(UserDefinedType):
    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"VECTOR({self.dim})"


class _PgVectorFallback(UserDefinedType):
    """Used only if the pgvector package is missing; text I/O in pgvector's '[1,2,3]' format."""

    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dim})"


class Embedding(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dim))
            except ImportError:  # pragma: no cover
                return dialect.type_descriptor(_PgVectorFallback(self.dim))
        if dialect.name == "mssql":
            return dialect.type_descriptor(_MssqlVector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect):  # type: ignore[override]
        if value is None:
            return None
        vec = [float(x) for x in value]
        if len(vec) != self.dim:
            raise ValueError(f"embedding must have {self.dim} dims, got {len(vec)}")
        if dialect.name == "postgresql":
            try:
                import pgvector.sqlalchemy  # noqa: F401

                return vec
            except ImportError:  # pragma: no cover
                return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
        return json.dumps([round(x, 7) for x in vec])

    def process_result_value(self, value: Any, dialect):  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, str):
            return [float(x) for x in json.loads(value)]
        return [float(x) for x in value]
