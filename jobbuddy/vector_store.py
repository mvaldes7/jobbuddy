"""Vector search over jobs.embedding with dialect-specific backends.

* PgVectorStore  - pgvector ``<=>`` cosine distance (uses the HNSW vector_cosine_ops index)
* MssqlVectorStore - Azure SQL ``VECTOR_DISTANCE('cosine', ...)``
* PythonVectorStore - SQLite/dev: loads candidate vectors and computes cosine in numpy
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jobbuddy.db.models import Job


@dataclass
class VectorHit:
    job_id: int
    similarity: float  # cosine similarity in [-1, 1]


class VectorStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def set_embedding(self, job: Job, vector: Sequence[float]) -> None:
        job.embedding = list(vector)

    def search(self, query: Sequence[float], k: int = 50, job_ids: Sequence[int] | None = None) -> list[VectorHit]:
        raise NotImplementedError

    def similarity_for(self, query: Sequence[float], job_ids: Sequence[int]) -> dict[int, float]:
        return {h.job_id: h.similarity for h in self.search(query, k=len(job_ids) or 1, job_ids=job_ids)}


class PythonVectorStore(VectorStore):
    def search(self, query, k=50, job_ids=None):
        stmt = select(Job.id, Job.embedding).where(Job.embedding.is_not(None))
        if job_ids is not None:
            stmt = stmt.where(Job.id.in_(list(job_ids)))
        rows = self.session.execute(stmt).all()
        if not rows:
            return []
        q = np.asarray(query, dtype=float)
        q = q / (np.linalg.norm(q) or 1.0)
        mat = np.asarray([r[1] for r in rows], dtype=float)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ q) / norms
        order = np.argsort(-sims)[:k]
        return [VectorHit(int(rows[i][0]), float(sims[i])) for i in order]


class PgVectorStore(VectorStore):
    def search(self, query, k=50, job_ids=None):
        qv = "[" + ",".join(f"{float(x):.7g}" for x in query) + "]"
        where = "embedding IS NOT NULL"
        params: dict = {"q": qv, "k": k}
        if job_ids is not None:
            if not job_ids:
                return []
            where += " AND id = ANY(:ids)"
            params["ids"] = list(job_ids)
        sql = text(f"SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS sim FROM jobs "
                   f"WHERE {where} ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k")
        return [VectorHit(int(r[0]), float(r[1])) for r in self.session.execute(sql, params)]


class MssqlVectorStore(VectorStore):
    def search(self, query, k=50, job_ids=None):
        qv = json.dumps([round(float(x), 7) for x in query])
        where = "embedding IS NOT NULL"
        params: dict = {"q": qv, "k": k}
        if job_ids is not None:
            if not job_ids:
                return []
            ids = ",".join(str(int(i)) for i in job_ids)  # ints only; safe to inline
            where += f" AND id IN ({ids})"
        sql = text(
            f"SELECT TOP (:k) id, 1 - VECTOR_DISTANCE('cosine', embedding, CAST(:q AS VECTOR(384))) AS sim "
            f"FROM jobs WHERE {where} "
            f"ORDER BY VECTOR_DISTANCE('cosine', embedding, CAST(:q AS VECTOR(384)))"
        )
        return [VectorHit(int(r[0]), float(r[1])) for r in self.session.execute(sql, params)]


def get_vector_store(session: Session) -> VectorStore:
    name = session.get_bind().dialect.name
    if name == "postgresql":
        return PgVectorStore(session)
    if name == "mssql":
        return MssqlVectorStore(session)
    return PythonVectorStore(session)
