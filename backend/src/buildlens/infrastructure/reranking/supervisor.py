"""Dedicated spawned CPU worker for bounded offline cross-encoder inference."""

from __future__ import annotations

import importlib.metadata
import math
import multiprocessing
import os
import sys
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from buildlens.application.context import ContextInputError, ContextUnavailableError
from buildlens.application.embeddings import ModelCacheStatus
from buildlens.domain.context import RerankerContract, RerankResult
from buildlens.domain.processing import Chunk
from buildlens.infrastructure.reranking.model_cache import (
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


def local_contract(revision: str = MODEL_REVISION, batch_size: int = 8) -> RerankerContract:
    return RerankerContract(
        provider="buildlens.transformers-cross-encoder",
        provider_version="1.0.0",
        model_id=MODEL_ID,
        model_revision=revision,
        tokenizer_id=MODEL_ID,
        tokenizer_revision=revision,
        backend="pytorch",
        device="cpu",
        precision="float32",
        input_format="query-passage-pair.v1",
        sequence_limit=512,
        query_token_limit=128,
        window_policy="consecutive-non-overlapping-passage-token-ids.v1",
        aggregation="maximum-raw-logit-earliest-tie.v1",
        max_windows_per_candidate=16,
        batch_size=batch_size,
        transformers_version=_version("transformers"),
        torch_version=_version("torch"),
    )


def _worker(connection: Connection, model_path: str, batch_size: int) -> None:
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    tokenizer: Any = None
    model: Any = None
    try:
        while True:
            request = connection.recv()
            operation = request.get("operation")
            if operation == "close":
                return
            if operation == "load":
                try:
                    import torch
                    from transformers import AutoModelForSequenceClassification, AutoTokenizer

                    torch.set_num_threads(1)
                    torch.set_num_interop_threads(1)
                    tokenizer = AutoTokenizer.from_pretrained(
                        model_path, local_files_only=True, trust_remote_code=False
                    )
                    model = AutoModelForSequenceClassification.from_pretrained(
                        model_path,
                        local_files_only=True,
                        trust_remote_code=False,
                        use_safetensors=True,
                        torch_dtype=torch.float32,
                    ).to("cpu")
                    model.eval()
                    if int(model.config.num_labels) != 1:
                        raise RuntimeError
                    connection.send({"ok": True})
                except Exception:
                    connection.send({"ok": False, "code": "RERANKER_LOAD_FAILED"})
                continue
            if tokenizer is None or model is None:
                connection.send({"ok": False, "code": "RERANKER_NOT_LOADED"})
                continue
            try:
                connection.send(
                    {
                        "ok": True,
                        "results": _score(
                            tokenizer,
                            model,
                            cast(str, request["query"]),
                            cast(list[dict[str, str]], request["candidates"]),
                            batch_size,
                            lambda: connection.send({"progress": True}),
                        ),
                    }
                )
            except ValueError as error:
                connection.send({"ok": False, "code": str(error)})
            except Exception:
                connection.send({"ok": False, "code": "RERANKER_INFERENCE_FAILED"})
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


def _score(
    tokenizer: Any,
    model: Any,
    query: str,
    candidates: list[dict[str, str]],
    batch_size: int,
    progress: Any | None = None,
) -> list[dict[str, object]]:
    import torch

    query_ids = tokenizer(
        query,
        add_special_tokens=False,
        truncation=False,
        return_attention_mask=False,
        return_token_type_ids=False,
    )["input_ids"]
    if len(query_ids) > 128:
        raise ValueError("RERANKER_QUERY_TOKEN_LIMIT")
    passage_capacity = 512 - len(query_ids) - int(tokenizer.num_special_tokens_to_add(pair=True))
    if passage_capacity <= 0:
        raise ValueError("RERANKER_QUERY_TOKEN_LIMIT")
    results: list[dict[str, object]] = []
    flattened: list[tuple[int, int, list[int]]] = []
    valid: list[dict[str, object]] = []
    for candidate in candidates:
        passage_ids = tokenizer(
            candidate["content"],
            add_special_tokens=False,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )["input_ids"]
        windows = [
            passage_ids[index : index + passage_capacity]
            for index in range(0, len(passage_ids), passage_capacity)
        ] or [[]]
        if len(windows) > 16:
            results.append(
                {
                    "chunk_id": candidate["chunk_id"],
                    "score": None,
                    "window_count": 0,
                    "winning_window": None,
                    "exclusion_reason": "reranker_input_limit",
                }
            )
            continue
        valid_index = len(valid)
        valid.append(
            {
                "chunk_id": candidate["chunk_id"],
                "window_count": len(windows),
                "scores": [],
            }
        )
        flattened.extend(
            (valid_index, window_index, window) for window_index, window in enumerate(windows)
        )
    with torch.inference_mode():
        for start in range(0, len(flattened), batch_size):
            batch = flattened[start : start + batch_size]
            prepared = [
                tokenizer.prepare_for_model(
                    query_ids,
                    window,
                    add_special_tokens=True,
                    truncation=False,
                    return_attention_mask=True,
                    return_token_type_ids=True,
                )
                for _, _, window in batch
            ]
            features = tokenizer.pad(prepared, padding=True, return_tensors="pt")
            logits = model(**features).logits.reshape(-1).to(dtype=torch.float32).cpu().tolist()
            for (candidate_index, window_index, _), score in zip(batch, logits, strict=True):
                cast(list[tuple[int, float]], valid[candidate_index]["scores"]).append(
                    (window_index, float(score))
                )
            if progress:
                progress()
    for value in valid:
        scores = cast(list[tuple[int, float]], value["scores"])
        winning_window, score = max(scores, key=lambda item: (item[1], -item[0]))
        results.append(
            {
                "chunk_id": value["chunk_id"],
                "score": score,
                "window_count": value["window_count"],
                "winning_window": winning_window,
                "exclusion_reason": None,
            }
        )
    order = {item["chunk_id"]: index for index, item in enumerate(candidates)}
    return sorted(results, key=lambda item: order[cast(str, item["chunk_id"])])


class SupervisedCrossEncoder:
    def __init__(
        self,
        cache_root: Path,
        *,
        revision: str = MODEL_REVISION,
        model_load_timeout_seconds: float = 60,
        batch_timeout_seconds: float = 45,
        batch_size: int = 8,
        max_rss_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        if (
            len(revision) != 40
            or any(value not in "0123456789abcdef" for value in revision)
            or min(model_load_timeout_seconds, batch_timeout_seconds, batch_size, max_rss_bytes)
            <= 0
        ):
            raise ValueError("Reranker configuration is invalid.")
        self._path = snapshot_path(cache_root, revision)
        self._contract = local_contract(revision, batch_size)
        self._load_timeout = model_load_timeout_seconds
        self._batch_timeout = batch_timeout_seconds
        self._batch_size = batch_size
        self._max_rss = max_rss_bytes
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._loaded = False

    @property
    def contract(self) -> RerankerContract:
        return self._contract

    def status(self) -> ModelCacheStatus:
        available, reason = verify_snapshot(self._path, self._contract.model_revision)
        if sys.version_info[:2] != (3, 11):
            return ModelCacheStatus(False, str(self._path), "RERANKER_PYTHON_UNSUPPORTED")
        if "not-installed" in (
            self._contract.transformers_version,
            self._contract.torch_version,
            _version("psutil"),
        ):
            return ModelCacheStatus(False, str(self._path), "RERANKER_DEPENDENCIES_MISSING")
        return ModelCacheStatus(available, str(self._path), reason)

    def score(
        self, query: str, candidates: list[Chunk], timeout_seconds: float
    ) -> list[RerankResult]:
        if timeout_seconds <= 0:
            raise ContextUnavailableError("Reranking timed out.")
        if not candidates:
            return []
        deadline = time.monotonic() + timeout_seconds
        if not self._loaded:
            self._start(deadline)
        assert self._connection is not None
        payload = [{"chunk_id": str(item.id), "content": item.content} for item in candidates]
        try:
            self._connection.send({"operation": "score", "query": query, "candidates": payload})
            response = self._receive(
                min(self._batch_timeout, max(0.001, deadline - time.monotonic()))
            )
        except (BrokenPipeError, EOFError, OSError) as error:
            self._replace()
            raise ContextUnavailableError("Reranking worker stopped unexpectedly.") from error
        if not response.get("ok"):
            code = response.get("code")
            if code == "RERANKER_QUERY_TOKEN_LIMIT":
                raise ContextInputError("Query exceeds the reranker token limit.")
            self._replace()
            raise ContextUnavailableError("Reranking inference failed.")
        raw = cast(list[dict[str, object]], response.get("results", []))
        try:
            values = [
                RerankResult(
                    UUID(cast(str, value["chunk_id"])),
                    cast(float | None, value["score"]),
                    cast(int, value["window_count"]),
                    cast(int | None, value["winning_window"]),
                    cast(Any, value.get("exclusion_reason")),
                )
                for value in raw
            ]
        except (KeyError, TypeError, ValueError) as error:
            self._replace()
            raise ContextUnavailableError("Reranker returned malformed output.") from error
        expected = {item.id for item in candidates}
        if len(values) != len(candidates) or {item.chunk_id for item in values} != expected:
            self._replace()
            raise ContextUnavailableError("Reranker returned malformed output.")
        for value in values:
            excluded = value.exclusion_reason == "reranker_input_limit"
            valid_score = (
                type(value.score) is float
                and math.isfinite(value.score)
                and type(value.window_count) is int
                and 1 <= value.window_count <= 16
                and type(value.winning_window) is int
                and 0 <= value.winning_window < value.window_count
            )
            if not (
                (excluded and value.score is None and value.window_count == 0)
                or (value.exclusion_reason is None and valid_score)
            ):
                self._replace()
                raise ContextUnavailableError("Reranker returned malformed output.")
        return values

    def _start(self, deadline: float) -> None:
        status = self.status()
        if not status.available:
            raise ContextUnavailableError("Pinned local reranker is unavailable.")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker,
            args=(child, str(self._path), self._batch_size),
            name="buildlens-reranker",
            daemon=True,
        )
        process.start()
        child.close()
        self._process, self._connection = process, parent
        try:
            parent.send({"operation": "load"})
            response = self._receive(
                min(self._load_timeout, max(0.001, deadline - time.monotonic()))
            )
        except (BrokenPipeError, EOFError, OSError) as error:
            self._replace()
            raise ContextUnavailableError("Reranker stopped during model load.") from error
        if not response.get("ok"):
            self._replace()
            raise ContextUnavailableError("Pinned local reranker could not be loaded.")
        self._loaded = True

    def _receive(self, timeout: float) -> dict[str, Any]:
        assert self._connection is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connection.poll(min(0.1, max(0.0, deadline - time.monotonic()))):
                response = cast(dict[str, Any], self._connection.recv())
                if response.get("progress"):
                    continue
                return response
            if self._process is None or not self._process.is_alive():
                raise EOFError
            rss = self._rss_bytes()
            if rss is None:
                self._replace()
                raise ContextUnavailableError("Reranker resource measurement is unavailable.")
            if rss > self._max_rss:
                self._replace()
                raise ContextUnavailableError("Reranking worker exceeded its memory budget.")
        self._replace()
        raise ContextUnavailableError("Reranking worker timed out.")

    def _rss_bytes(self) -> int | None:
        if self._process is None or self._process.pid is None:
            return None
        try:
            import psutil

            return int(psutil.Process(self._process.pid).memory_info().rss)
        except Exception:
            return None

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
            if process.is_alive():
                process.kill()
                process.join(timeout=1)

    def close(self) -> None:
        if self._connection is not None and self._process is not None and self._process.is_alive():
            try:
                self._connection.send({"operation": "close"})
                self._process.join(timeout=1)
            except (BrokenPipeError, OSError):
                pass
        self._replace()
