"""LS-2: official langchain-postgres PGVectorStore in an isolated environment.

Speaks the vendored Stage 12 wire protocol. Vectors are supplied by the parent (reused,
verified stored vectors); queries are embedded through the parent bridge. The only
credential present is BUILDLENS_LAB_PG_URL for the separate lab database.
"""

from __future__ import annotations

from buildlens_lab_pg_worker import guard

guard.install_from_environment()

import hashlib  # noqa: E402
import importlib.metadata  # noqa: E402
import os  # noqa: E402
import struct  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from typing import Any  # noqa: E402

from langchain_core.documents import Document  # noqa: E402
from langchain_core.embeddings import Embeddings  # noqa: E402
from langchain_postgres import PGEngine, PGVectorStore  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from buildlens_lab_pg_worker.wire import PROTOCOL, WireError, read_frame, write_frame  # noqa: E402


class Failure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode()).hexdigest()


def digest(vector: list[float]) -> str:
    return hashlib.sha256(struct.pack(f"<{len(vector)}f", *vector)).hexdigest()


class Session:
    def __init__(self, inp: Any, out: Any) -> None:
        self.inp, self.out, self.call_id, self.steps = inp, out, 0, []

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self.call_id += 1
        write_frame(self.out, {"kind": "call", "id": self.call_id, "method": method, "params": params})
        frame = read_frame(self.inp)
        if frame.get("kind") != "reply" or frame.get("id") != self.call_id:
            raise WireError("WIRE_SEQUENCE_INVALID")
        if not frame.get("ok"):
            raise Failure(str((frame.get("error") or {}).get("code", "BRIDGE_FAILED")))
        return frame["result"]

    def step(self, kind: str, name: str, started: float, attributes: dict[str, Any]) -> None:
        self.steps.append({"seq": len(self.steps) + 1, "kind": kind, "name": name, "framework_event": "langchain_postgres", "duration_ms": round((time.perf_counter() - started) * 1000, 3), "attributes": attributes, "item_refs": [], "parent": None})


class ReusedEmbeddings(Embeddings):
    """Documents: verified stored vectors by sha256(text) (strict). Queries: parent bridge."""

    def __init__(self, session: Session, reuse: dict[str, list[float]]) -> None:
        self.session, self.reuse, self.reused = session, reuse, 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for value in texts:
            vector = self.reuse.get(sha(value))
            if vector is None:
                raise Failure("VECTOR_REUSE_MISS")
            vectors.append(vector)
        self.reused += len(texts)
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return list(self.session.call("embed_query", {"text": query})["vector"])


STATE: dict[str, Any] = {"indexes": {}}


def build_begin(session: Session, params: dict[str, Any]) -> Any:
    STATE["pending"] = {"index_id": params["index_id"], "documents": [], "reuse": {}}
    return {"ok": True}


def add_documents(session: Session, params: dict[str, Any]) -> Any:
    pending = STATE["pending"]
    for raw in params["documents"]:
        pending["documents"].append(raw)
        if raw.get("vector") is not None:
            pending["reuse"][sha(raw["text"])] = [float(v) for v in raw["vector"]]
    return {"received": len(pending["documents"])}


def build_commit(session: Session, params: dict[str, Any]) -> Any:
    pending = STATE.pop("pending")
    url = os.environ.get("BUILDLENS_LAB_PG_URL")
    if not url:
        raise Failure("LAB_DATABASE_NOT_CONFIGURED")
    table = "lcpg_" + pending["index_id"][:16]
    embeddings = ReusedEmbeddings(session, pending["reuse"])
    started = time.perf_counter()
    engine = PGEngine.from_connection_string(url=url)
    engine.init_vectorstore_table(table, len(next(iter(pending["reuse"].values()))), schema_name="lab", overwrite_existing=True)
    store = PGVectorStore.create_sync(engine, embeddings, table, schema_name="lab")
    documents = [Document(page_content=d["text"], metadata=d["metadata"], id=None) for d in pending["documents"]]
    ids = [d["ref"] for d in pending["documents"]]
    for start in range(0, len(documents), 256):
        store.add_documents(documents[start : start + 256], ids=ids[start : start + 256])
    session.step("store", "PGVectorStore.add_documents", started, {"rows": len(ids)})
    raw = create_engine(make_url(url))
    with raw.connect() as connection:
        rows = connection.execute(text(f'SELECT langchain_id::text, embedding::text FROM lab."{table}"'))
        digests = {r[0]: digest([float(v) for v in r[1].strip("[]").split(",")]) for r in rows}
    raw.dispose()
    STATE["indexes"][pending["index_id"]] = {"store": store, "documents": {d["ref"]: d for d in pending["documents"]}}
    return {
        "nodes": len(ids),
        "table": f"lab.{table}",
        "vector_digests": digests,
        "embedding": {"source": "reuse-stored", "reused": embeddings.reused, "computed_via_bridge": 0, "reuse_misses": 0, "transformations": {"identity": len(ids)}},
        "embedding_records": [],
        "anchor_methods": {"native": len(ids)},
    }


def query(session: Session, params: dict[str, Any]) -> Any:
    index = STATE["indexes"].get(params.get("index_id"))
    if index is None:
        raise Failure("LAB_INDEX_NOT_LOADED")
    k = int(params["config"]["params"]["k"])
    started = time.perf_counter()
    hits = index["store"].similarity_search_with_score(params["question"], k=k)
    session.step("retrieve", "PGVectorStore.similarity_search_with_score", started, {"hits": len(hits)})
    items = []
    for rank, (document, distance) in enumerate(hits, 1):
        meta = document.metadata
        value = document.page_content
        items.append({
            "ref": str(document.id or meta.get("chunk_id")), "rank": rank, "text_sha256": sha(value), "text_bytes": len(value.encode()),
            "locator": meta.get("locator"), "artifact_fingerprint": meta.get("artifact_fingerprint"),
            "span": {"start_offset": meta.get("start_offset"), "end_offset": meta.get("end_offset")}, "anchored": True,
            "chunk_id": meta.get("chunk_id"), "score": 1.0 - float(distance), "score_kind": "cosine (1 - distance)",
            "detail": {"anchor_method": "native", "raw_distance": float(distance)},
        })
    return {"retrieved": items, "ranked": items, "candidates": items, "packed_refs": [], "info": {"store": "langchain-postgres PGVectorStore"}, "answer": None, "texts": {}}


HANDLERS = {"build_begin": build_begin, "add_documents": add_documents, "build_commit": build_commit, "query": query, "drop": lambda s, p: {}}


def main() -> None:
    inp = os.fdopen(os.dup(0), "rb", buffering=0)
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    versions = {p: importlib.metadata.version(p) for p in ("langchain-postgres", "langchain-core", "pgvector", "psycopg")}
    versions["python"] = sys.version.split()[0]
    write_frame(out, {"kind": "hello", "protocol": PROTOCOL, "framework": "langchain_postgres", "versions": versions, "pid": os.getpid(), "guard": guard.policy()})
    session = Session(inp, out)
    while True:
        try:
            frame = read_frame(inp)
        except WireError as error:
            if error.code == "WIRE_EOF":
                return
            raise
        request_id, op = frame.get("id"), str(frame.get("op"))
        session.steps = []
        if op == "shutdown":
            write_frame(out, {"kind": "response", "id": request_id, "ok": True, "result": {}})
            return
        try:
            value = HANDLERS[op](session, dict(frame.get("params") or {}))
            response = {"kind": "response", "id": request_id, "ok": True, "result": {"value": value, "steps": session.steps, "bridge_calls": {}, "guard_denials": guard.denials()}}
        except Failure as error:
            response = {"kind": "response", "id": request_id, "ok": False, "error": {"code": error.code, "message": ""}, "result": {}}
        except KeyError:
            response = {"kind": "response", "id": request_id, "ok": False, "error": {"code": "WORKER_OPERATION_UNKNOWN", "message": op}, "result": {}}
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            response = {"kind": "response", "id": request_id, "ok": False, "error": {"code": "WORKER_EXCEPTION", "message": type(error).__name__}, "result": {}}
        write_frame(out, response)


if __name__ == "__main__":
    main()
