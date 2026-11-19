"""SQLite caches for valid responses and individual embeddings."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Lock
from typing import Any

from .config import ModelRoute


class CacheCorruptionError(RuntimeError):
    """A cached row is damaged. Preserve it for investigation; do not call upstream."""

    def __init__(self, table: str, column: str, namespace: str, key: str, reason: str):
        self.table = table
        self.column = column
        self.namespace = namespace
        self.key = key
        self.reason = reason
        super().__init__(
            f"{table}.{column}: {reason}; namespace={namespace!r}; key={key}"
        )


def _hash_key(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_cache_key(
    *,
    model: str,
    instructions: str,
    input_messages: list[dict[str, str]],
    schema: dict[str, Any],
    parameters: dict[str, Any],
) -> str:
    return _hash_key(
        {
            "model": model,
            "messages": [{"role": "system", "content": instructions}, *input_messages],
            "schema": schema,
            "parameters": parameters,
        }
    )


def embedding_cache_key(model: str, text: str, route: ModelRoute | None = None) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Model name must be a non-empty string.")
    if not isinstance(text, str):
        raise TypeError("Text must be a string.")
    return _hash_key(
        {
            "endpoint": "embeddings",
            "model": model,
            "text": text,
            "backend": route.backend if route else None,
            "upstream_model": route.upstream_model if route else None,
            "base_url": None,
            "location": route.location if route else None,
            "embedding_dimensions": route.embedding_dimensions if route else None,
        }
    )


@dataclass(frozen=True)
class ResponseCacheEntry:
    namespace: str
    key: str
    text: str
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_final: int | None
    tokens_out_final: int | None
    usage_complete: bool
    latency_s: float
    attempts: int
    parse_retries: int
    provider_response: dict[str, Any]


@dataclass(frozen=True)
class EmbeddingCacheEntry:
    vector: list[float]
    tokens_in: int | None
    latency_s: float | None
    usage_complete: bool
    attempts: int


class GatewayCache:
    """One SQLite connection, with separate keys and tables for each endpoint."""

    def __init__(self, database_path: str | Path):
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS response_cache (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    text TEXT NOT NULL,
                    tokens_in INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
                    tokens_out INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
                    tokens_in_final INTEGER CHECK (tokens_in_final IS NULL OR tokens_in_final >= 0),
                    tokens_out_final INTEGER CHECK (tokens_out_final IS NULL OR tokens_out_final >= 0),
                    usage_complete INTEGER NOT NULL CHECK (usage_complete IN (0, 1)),
                    latency_s REAL NOT NULL CHECK (latency_s >= 0),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    parse_retries INTEGER NOT NULL CHECK (parse_retries >= 0),
                    provider_response_json TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    tokens_in INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
                    latency_s REAL CHECK (latency_s IS NULL OR latency_s >= 0),
                    usage_complete INTEGER NOT NULL CHECK (usage_complete IN (0, 1)),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    PRIMARY KEY (namespace, key)
                )
            """)

    def get_response(self, namespace: str, key: str) -> ResponseCacheEntry | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM response_cache WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None

        # Reject malformed cached payloads instead of treating them as cache misses.
        try:
            provider_response = json.loads(row["provider_response_json"])
        except (ValueError, TypeError) as exc:
            raise CacheCorruptionError(
                "response_cache", "provider_response_json", namespace, key,
                f"invalid JSON ({type(exc).__name__})",
            ) from exc
        if not isinstance(provider_response, dict):
            raise CacheCorruptionError(
                "response_cache", "provider_response_json", namespace, key,
                "expected a JSON object",
            )

        return ResponseCacheEntry(
            namespace=row["namespace"],
            key=row["key"],
            text=row["text"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            tokens_in_final=row["tokens_in_final"],
            tokens_out_final=row["tokens_out_final"],
            usage_complete=bool(row["usage_complete"]),
            latency_s=row["latency_s"],
            attempts=row["attempts"],
            parse_retries=row["parse_retries"],
            provider_response=provider_response,
        )

    def put_response(self, entry: ResponseCacheEntry) -> bool:
        if not isinstance(entry.provider_response, dict):
            raise TypeError("provider_response must be a dictionary.")

        provider_response_json = (
            json.dumps(
                entry.provider_response,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if entry.provider_response is not None
            else None
        )

        # Keep the first stored response if concurrent requests reach this key.
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO response_cache
                (namespace, key, text, tokens_in, tokens_out, tokens_in_final,
                 tokens_out_final, usage_complete, latency_s, attempts, parse_retries, provider_response_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    entry.namespace,
                    entry.key,
                    entry.text,
                    entry.tokens_in,
                    entry.tokens_out,
                    entry.tokens_in_final,
                    entry.tokens_out_final,
                    int(entry.usage_complete),
                    entry.latency_s,
                    entry.attempts,
                    entry.parse_retries,
                    provider_response_json,
                ),
            )
            return cursor.rowcount == 1

    def get_embedding(
        self, namespace: str, model: str, text: str, route: ModelRoute | None = None
    ) -> EmbeddingCacheEntry | None:
        key = embedding_cache_key(model, text, route)
        with self._lock:
            row = self._db.execute(
                "SELECT vector_json, tokens_in, latency_s, usage_complete, attempts "
                "FROM embedding_cache WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        # A cached vector must be valid for the configured embedding model.
        try:
            vector = json.loads(row["vector_json"])
        except (ValueError, TypeError) as exc:
            raise CacheCorruptionError(
                "embedding_cache", "vector_json", namespace, key,
                f"invalid JSON ({type(exc).__name__})",
            ) from exc
        if (
            not isinstance(vector, list) or not vector
            or any(type(x) not in (int, float) or not isfinite(x) for x in vector)
        ):
            raise CacheCorruptionError(
                "embedding_cache", "vector_json", namespace, key,
                "expected a non-empty vector of finite numbers",
            )
        if route is not None and route.embedding_dimensions is not None and len(vector) != route.embedding_dimensions:
            raise CacheCorruptionError(
                "embedding_cache", "vector_json", namespace, key,
                f"expected {route.embedding_dimensions} dimensions, found {len(vector)}",
            )
        return EmbeddingCacheEntry(
            vector=vector,
            tokens_in=row["tokens_in"],
            latency_s=row["latency_s"],
            usage_complete=bool(row["usage_complete"]),
            attempts=row["attempts"],
        )

    def put_embedding(
        self,
        namespace: str,
        model: str,
        text: str,
        vector: list[float],
        *,
        tokens_in: int | None = None,
        latency_s: float | None = None,
        usage_complete: bool = False,
        attempts: int = 0,
        route: ModelRoute | None = None,
    ) -> None:
        key = embedding_cache_key(model, text, route)
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("Namespace must be a non-empty string.")
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not isfinite(v)
                for v in vector
            )
        ):
            raise ValueError("Vector must be a non-empty list of finite numbers.")
        if tokens_in is not None and (type(tokens_in) is not int or tokens_in < 0):
            raise ValueError("Invalid tokens_in")
        if latency_s is not None and (
            isinstance(latency_s, bool)
            or not isinstance(latency_s, (int, float))
            or not isfinite(latency_s)
            or latency_s < 0
        ):
            raise ValueError("latency_s must be a non-negative finite number or None.")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("attempts must be a non-negative integer.")
        vector_json = json.dumps(vector, separators=(",", ":"), allow_nan=False)
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO embedding_cache
                (namespace, key, vector_json, tokens_in, latency_s, usage_complete, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    namespace,
                    key,
                    vector_json,
                    tokens_in,
                    latency_s,
                    int(usage_complete),
                    attempts,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
