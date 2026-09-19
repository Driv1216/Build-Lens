"""Reusable supervised local Sentence Transformers subprocess."""

from __future__ import annotations

import importlib.metadata
import multiprocessing
import os
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, cast

from buildlens.application.embeddings import (
    EmbeddingInputError,
    EmbeddingLimitError,
    EmbeddingUnavailableError,
    ModelCacheStatus,
)
from buildlens.domain.embeddings import EmbeddingContract, EmbeddingVector
from buildlens.infrastructure.embeddings.model_cache import (
    MODEL_ID,
    MODEL_REVISION,
    snapshot_path,
    verify_snapshot,
)


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def local_contract(revision: str = MODEL_REVISION) -> EmbeddingContract:
    return EmbeddingContract(
        provider="buildlens.sentence-transformers",
        provider_version="1.0.0",
        model_id=MODEL_ID,
        model_revision=revision,
        dimensions=384,
        backend="pytorch",
        device="cpu",
        precision="float32",
        document_prompt="empty",
        query_prompt="empty",
        document_format="chunk-content.v1",
        query_format="query-text.v1",
        token_limit=256,
        window_policy="consecutive-non-overlapping-token-ids.v1",
        pooling="token-windows-weighted-mean.v1",
        normalization="l2.v1",
        sentence_transformers_version=_version("sentence-transformers"),
        transformers_version=_version("transformers"),
        torch_version=_version("torch"),
    )


def _worker(connection: Connection, model_path: str, window_batch_size: int) -> None:
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    model: Any = None
    try:
        while True:
            request = connection.recv()
            operation = request.get("operation")
            if operation == "close":
                return
            if operation == "load":
                try:
                    from sentence_transformers import SentenceTransformer

                    model = SentenceTransformer(
                        model_path,
                        device="cpu",
                        backend="torch",
                        local_files_only=True,
                        trust_remote_code=False,
                    )
                    model.max_seq_length = 256
                    model.eval()
                    if model.get_sentence_embedding_dimension() != 384:
                        raise RuntimeError
                    connection.send({"ok": True})
                except Exception:
                    connection.send({"ok": False, "code": "MODEL_LOAD_FAILED"})
                continue
            if model is None:
                connection.send({"ok": False, "code": "MODEL_NOT_LOADED"})
                continue
            try:
                texts = cast(list[str], request["texts"])
                query = operation == "query"
                vectors = _encode(
                    model,
                    texts,
                    window_batch_size,
                    query,
                    lambda: connection.send({"progress": True}),
                )
                connection.send({"ok": True, "vectors": vectors})
            except ValueError as error:
                connection.send({"ok": False, "code": str(error)})
            except Exception:
                connection.send({"ok": False, "code": "INFERENCE_FAILED"})
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


def _encode(
    model: Any,
    texts: list[str],
    window_batch_size: int,
    query: bool,
    progress: Any | None = None,
) -> list[dict[str, object]]:
    import torch

    tokenizer = model.tokenizer
    required_special = int(tokenizer.num_special_tokens_to_add(pair=False))
    payload_limit = 256 - required_special
    documents: list[list[list[int]]] = []
    for text in texts:
        token_ids = tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )["input_ids"]
        if query and len(token_ids) > payload_limit:
            raise ValueError("QUERY_TOKEN_LIMIT")
        windows = [
            token_ids[index : index + payload_limit]
            for index in range(0, len(token_ids), payload_limit)
        ]
        if not windows:
            windows = [[]]
        if len(windows) > 32:
            raise ValueError("DOCUMENT_WINDOW_LIMIT")
        documents.append(windows)

    flattened: list[tuple[int, list[int], int]] = []
    for document_index, windows in enumerate(documents):
        flattened.extend((document_index, window, max(1, len(window))) for window in windows)
    sums = [torch.zeros(384, dtype=torch.float32) for _ in documents]
    weights = [0 for _ in documents]
    with torch.inference_mode():
        for start in range(0, len(flattened), window_batch_size):
            batch = flattened[start : start + window_batch_size]
            prepared = [
                tokenizer.prepare_for_model(
                    token_ids,
                    add_special_tokens=True,
                    truncation=False,
                    return_attention_mask=True,
                    return_token_type_ids=True,
                )
                for _, token_ids, _ in batch
            ]
            features = tokenizer.pad(prepared, padding=True, return_tensors="pt")
            encoded = model(features)["sentence_embedding"].to(dtype=torch.float32)
            encoded = torch.nn.functional.normalize(encoded, p=2, dim=1)
            for position, (document_index, _, weight) in enumerate(batch):
                sums[document_index] += encoded[position].cpu() * weight
                weights[document_index] += weight
            if progress is not None:
                progress()
    result: list[dict[str, object]] = []
    for index, total in enumerate(sums):
        pooled = torch.nn.functional.normalize(total / weights[index], p=2, dim=0)
        result.append(
            {
                "values": [float(value) for value in pooled.tolist()],
                "window_count": len(documents[index]),
            }
        )
    return result


class SupervisedSentenceTransformer:
    def __init__(
        self,
        cache_root: Path,
        *,
        revision: str = MODEL_REVISION,
        model_load_timeout_seconds: float = 60,
        batch_timeout_seconds: float = 30,
        window_batch_size: int = 16,
        max_rss_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        if (
            len(revision) != 40
            or any(value not in "0123456789abcdef" for value in revision)
            or min(
                model_load_timeout_seconds,
                batch_timeout_seconds,
                window_batch_size,
                max_rss_bytes,
            )
            <= 0
        ):
            raise ValueError("Embedding provider configuration is invalid.")
        self._path = snapshot_path(cache_root, revision)
        self._contract = local_contract(revision)
        self._load_timeout = model_load_timeout_seconds
        self._batch_timeout = batch_timeout_seconds
        self._batch_size = window_batch_size
        self._max_rss = max_rss_bytes
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._loaded = False

    @property
    def contract(self) -> EmbeddingContract:
        return self._contract

    def status(self) -> ModelCacheStatus:
        available, reason = verify_snapshot(self._path, self._contract.model_revision)
        dependencies = (
            self._contract.sentence_transformers_version,
            self._contract.transformers_version,
            self._contract.torch_version,
        )
        if "not-installed" in dependencies:
            return ModelCacheStatus(False, str(self._path), "EMBEDDING_DEPENDENCIES_MISSING")
        return ModelCacheStatus(available, str(self._path), reason)

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        if not texts:
            return []
        return self._request("documents", texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._request("query", [text])[0]

    def embed_query_with_timeout(self, text: str, timeout_seconds: float) -> EmbeddingVector:
        if timeout_seconds <= 0:
            raise EmbeddingUnavailableError("Embedding query timed out.")
        return self._request("query", [text], timeout_seconds=timeout_seconds)[0]

    def _start(self, deadline: float | None = None) -> None:
        status = self.status()
        if not status.available:
            raise EmbeddingUnavailableError("Pinned local model is unavailable.")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker,
            args=(child, str(self._path), self._batch_size),
            name="buildlens-embeddings",
            daemon=True,
        )
        process.start()
        child.close()
        self._process, self._connection = process, parent
        try:
            parent.send({"operation": "load"})
            timeout = self._load_timeout
            if deadline is not None:
                timeout = min(timeout, max(0.001, deadline - time.monotonic()))
            response = self._receive(timeout, reset_on_progress=deadline is None)
        except (BrokenPipeError, EOFError, OSError) as error:
            self._replace()
            raise EmbeddingUnavailableError(
                "Embedding worker stopped during model load."
            ) from error
        if not response.get("ok"):
            self._replace()
            raise EmbeddingUnavailableError("Pinned local model could not be loaded.")
        self._loaded = True

    def _request(
        self, operation: str, texts: list[str], *, timeout_seconds: float | None = None
    ) -> list[EmbeddingVector]:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        if not self._loaded:
            self._start(deadline)
        assert self._connection is not None
        try:
            self._connection.send({"operation": operation, "texts": texts})
            timeout = self._batch_timeout
            if deadline is not None:
                timeout = min(timeout, max(0.001, deadline - time.monotonic()))
            response = self._receive(timeout, reset_on_progress=deadline is None)
        except (BrokenPipeError, EOFError, OSError) as error:
            self._replace()
            raise EmbeddingUnavailableError("Embedding worker stopped unexpectedly.") from error
        if not response.get("ok"):
            code = response.get("code")
            if code == "QUERY_TOKEN_LIMIT":
                raise EmbeddingInputError("Query exceeds the model payload-token limit.")
            if code == "DOCUMENT_WINDOW_LIMIT":
                raise EmbeddingLimitError("A chunk exceeds the configured window limit.")
            self._replace()
            raise EmbeddingUnavailableError("Embedding inference failed.")
        raw = cast(list[dict[str, object]], response.get("vectors", []))
        return [
            EmbeddingVector(
                tuple(float(value) for value in cast(list[float], item["values"])),
                int(cast(int, item["window_count"])),
            )
            for item in raw
        ]

    def _receive(self, timeout: float, *, reset_on_progress: bool = True) -> dict[str, Any]:
        assert self._connection is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connection.poll(min(0.1, max(0.0, deadline - time.monotonic()))):
                response = cast(dict[str, Any], self._connection.recv())
                if response.get("progress") and reset_on_progress:
                    deadline = time.monotonic() + timeout
                    continue
                if response.get("progress"):
                    continue
                return response
            if self._process is None or not self._process.is_alive():
                raise EOFError
            if self._rss_bytes() > self._max_rss:
                self._replace()
                raise EmbeddingUnavailableError("Embedding worker exceeded its memory budget.")
        self._replace()
        raise EmbeddingUnavailableError("Embedding worker timed out.")

    def _rss_bytes(self) -> int:
        if self._process is None or self._process.pid is None:
            return 0
        try:
            import psutil

            return int(psutil.Process(self._process.pid).memory_info().rss)
        except Exception:
            return 0

    def _replace(self) -> None:
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        self._loaded = False
        if connection is not None:
            connection.close()
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=1)

    def close(self) -> None:
        if self._connection is not None and self._process is not None and self._process.is_alive():
            try:
                self._connection.send({"operation": "close"})
                self._process.join(timeout=1)
            except (BrokenPipeError, OSError):
                pass
        self._replace()
