"""LS-1: a hand-written LangChain `VectorStore` on pgvector 0.4.1 (lab database only).

Exists because the official `langchain-postgres` requires `pgvector<0.4` and cannot share
BuildLens's environment. Implementing the contract directly shows exactly what the
abstraction asks of a store: add, exact cosine search (with deterministic ties), MMR,
get-by-id and delete. The connection URL comes only from BUILDLENS_LAB_PG_URL, which the
parent injects for lab-database runs; production credentials are never present.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore
from langchain_core.vectorstores.utils import maximal_marginal_relevance
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB

from buildlens.domain.lab import LabError

_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


def lab_url() -> str:
    url = os.environ.get("BUILDLENS_LAB_PG_URL")
    if not url:
        raise LabError("LAB_DATABASE_NOT_CONFIGURED")
    return url


class BuildLensLabPgVectorStore(VectorStore):
    def __init__(
        self, embedding: Embeddings, table: str, dimensions: int, url: str | None = None
    ) -> None:
        if not _TABLE.match(table):
            raise LabError("LAB_TABLE_NAME_INVALID")
        self._embedding = embedding
        self._engine = create_engine(url or lab_url(), pool_pre_ping=True)
        self._table = Table(
            table,
            MetaData(schema="lab"),
            Column("id", String(80), primary_key=True),
            Column("content", Text, nullable=False),
            Column("metadata", JSONB, nullable=False),
            Column("embedding", Vector(dimensions), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    @property
    def embeddings(self) -> Embeddings:
        return self._embedding

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        values = list(texts)
        if ids is None or len(ids) != len(values):
            raise LabError(
                "LAB_IDS_REQUIRED", "Explicit ids keep identical-content chunks distinct"
            )
        vectors = self._embedding.embed_documents(values)
        rows = [
            {
                "id": ids[i],
                "content": values[i],
                "metadata": (metadatas or [{}] * len(values))[i],
                "embedding": vectors[i],
            }
            for i in range(len(values))
        ]
        with self._engine.begin() as connection:
            for start in range(0, len(rows), 500):
                connection.execute(self._table.insert(), rows[start : start + 500])
        return ids

    def _search(self, embedding: list[float], k: int, with_vectors: bool = False) -> list[Any]:
        distance = self._table.c.embedding.cosine_distance(embedding)
        columns = [
            self._table.c.id,
            self._table.c.content,
            self._table.c.metadata,
            distance.label("distance"),
        ]
        if with_vectors:
            columns.append(self._table.c.embedding)
        statement = select(*columns).order_by(distance, self._table.c.id).limit(k)
        with self._engine.connect() as connection:
            return list(connection.execute(statement))

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int = 4, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        return [
            (
                Document(page_content=row.content, metadata=dict(row.metadata), id=row.id),
                1.0 - float(row.distance),
            )
            for row in self._search(embedding, k)
        ]

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        return self.similarity_search_with_score_by_vector(self._embedding.embed_query(query), k)

    def similarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        return [d for d, _ in self.similarity_search_with_score(query, k)]

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: list[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> list[Document]:
        rows = self._search(embedding, fetch_k, with_vectors=True)
        np = importlib.import_module("numpy")  # dynamic: numpy stubs target Python >= 3.12
        chosen = maximal_marginal_relevance(
            np.array(embedding, dtype=np.float32),
            [list(r.embedding) for r in rows],
            lambda_mult=lambda_mult,
            k=k,
        )
        return [
            Document(page_content=rows[i].content, metadata=dict(rows[i].metadata), id=rows[i].id)
            for i in chosen
        ]

    def get_by_ids(self, ids: Sequence[str], /) -> list[Document]:
        with self._engine.connect() as connection:
            rows = connection.execute(select(self._table).where(self._table.c.id.in_(list(ids))))
            return [
                Document(page_content=r.content, metadata=dict(r.metadata), id=r.id) for r in rows
            ]

    def delete(self, ids: list[str] | None = None, **kwargs: Any) -> bool | None:
        with self._engine.begin() as connection:
            statement = delete(self._table)
            if ids is not None:
                statement = statement.where(self._table.c.id.in_(ids))
            connection.execute(statement)
        return True

    def count(self) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.execute(select(func.count()).select_from(self._table)).scalar_one()
            )

    def drop(self) -> None:
        self._table.drop(self._engine, checkfirst=True)
        self._engine.dispose()

    @classmethod
    def from_texts(
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BuildLensLabPgVectorStore:
        store = cls(embedding, kwargs.pop("table"), kwargs.pop("dimensions"))
        store.add_texts(texts, metadatas, ids=kwargs.get("ids"))
        return store
