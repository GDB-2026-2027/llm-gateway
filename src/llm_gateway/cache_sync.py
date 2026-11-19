"""Incremental SQLite cache synchronization using Backblaze B2's S3 API.

Commands (from the project root):
    uv run python -m llm_gateway.cache_sync status
    uv run python -m llm_gateway.cache_sync pull
    uv run python -m llm_gateway.cache_sync push

Only the two cache tables are synchronized. Run logs are not uploaded.
Configuration is read from gateway.toml and secrets from b2.local.env.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from dotenv import dotenv_values
from filelock import FileLock

from .cache import GatewayCache
from .config import load_config

FORMAT = "semops-cache-v1"
TABLES = {"responses": "response_cache", "embeddings": "embedding_cache"}
DEFAULT_MAX_ENTRIES = 1000
DEFAULT_MAX_ARCHIVE_MB = 8
MIB = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_json(obj: Any) -> str:
    return hashlib.sha256(_json(obj).encode("utf-8")).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", value
    ):
        raise ValueError(
            f"{field} must contain letters, digits, dots, underscores or hyphens"
        )
    return value


@dataclass(frozen=True)
class SyncSettings:
    database_path: Path
    state_dir: Path
    bucket: str
    endpoint: str
    student_id: str
    compatibility_id: str
    access_key_id: str
    secret_access_key: str
    max_entries_per_archive: int = DEFAULT_MAX_ENTRIES
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_MB * MIB


def load_settings(path: Path) -> SyncSettings:
    path = path.expanduser().resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)

    section = raw.get("cache_sync")
    if not isinstance(section, dict) or section.get("enabled") is not True:
        raise ValueError("Set [cache_sync] enabled = true in gateway.toml")
    if section.get("provider", "backblaze_b2") != "backblaze_b2":
        raise ValueError("cache_sync.provider must be 'backblaze_b2'")

    bucket = section.get("bucket")
    endpoint = section.get("endpoint")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("cache_sync.bucket must be a non-empty string")
    if not isinstance(endpoint, str):
        raise TypeError("cache_sync.endpoint must be a Backblaze HTTPS URL")

    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"s3\.[a-z0-9-]+\.backblazeb2\.com", host)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Expected an endpoint like https://s3.eu-central-003.backblazeb2.com"
        )

    if "student_id" in section:
        raise ValueError(
            "Remove cache_sync.student_id from gateway.toml; "
            "set SEMOPS_STUDENT_ID in the local B2 credentials file instead"
        )
    compatibility_id = _safe_id(section.get("compatibility_id"), "compatibility_id")

    credentials_file = section.get("credentials_file", "b2.local.env")
    if not isinstance(credentials_file, str) or not credentials_file.strip():
        raise ValueError("cache_sync.credentials_file must be a file path")
    credentials_path = Path(credentials_file).expanduser()
    if not credentials_path.is_absolute():
        credentials_path = path.parent / credentials_path
    if not credentials_path.is_file():
        raise FileNotFoundError(f"Missing B2 credentials file: {credentials_path}")

    creds = dotenv_values(credentials_path)
    student_id = _safe_id(creds.get("SEMOPS_STUDENT_ID"), "SEMOPS_STUDENT_ID")
    access_key_id = creds.get("B2_ACCESS_KEY_ID")
    secret_access_key = creds.get("B2_SECRET_ACCESS_KEY")
    if not isinstance(access_key_id, str) or not access_key_id.strip():
        raise ValueError("Missing B2_ACCESS_KEY_ID in credentials file")
    if not isinstance(secret_access_key, str) or not secret_access_key.strip():
        raise ValueError("Missing B2_SECRET_ACCESS_KEY in credentials file")

    entries = section.get("max_entries_per_archive", DEFAULT_MAX_ENTRIES)
    if type(entries) is not int or entries <= 0:
        raise ValueError(
            "cache_sync.max_entries_per_archive must be a positive integer"
        )

    archive_mb = section.get("max_archive_mb", DEFAULT_MAX_ARCHIVE_MB)
    if type(archive_mb) not in (int, float) or archive_mb <= 0:
        raise ValueError("cache_sync.max_archive_mb must be a positive, finite number")
    if isinstance(archive_mb, float) and not math.isfinite(archive_mb):
        raise ValueError("cache_sync.max_archive_mb must be finite")
    # Keep large integer limits exact; converting to float would lose precision.
    archive_bytes = max(1, math.ceil(archive_mb * MIB))

    gateway_config = load_config(path)
    return SyncSettings(
        database_path=Path(gateway_config.database_path),
        state_dir=path.parent / ".cache_sync",
        bucket=bucket.strip(),
        endpoint=f"https://{host}",
        student_id=student_id,
        compatibility_id=compatibility_id,
        access_key_id=access_key_id.strip(),
        secret_access_key=secret_access_key.strip(),
        max_entries_per_archive=entries,
        max_archive_bytes=archive_bytes,
    )


def s3_client(s: SyncSettings) -> Any:
    host = urlsplit(s.endpoint).hostname or ""
    match = re.fullmatch(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", host)
    if match is None:
        raise ValueError("Invalid Backblaze S3 endpoint")
    return boto3.client(
        "s3",
        endpoint_url=s.endpoint,
        region_name=match.group(1),
        aws_access_key_id=s.access_key_id,
        aws_secret_access_key=s.secret_access_key,
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 4, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _transfer_config() -> TransferConfig:
    # Multipart thresholds tune transfers; the archive target is checked separately.
    return TransferConfig(
        multipart_threshold=64 * MIB,
        multipart_chunksize=16 * MIB,
        max_concurrency=2,
    )


def _open_db(s: SyncSettings) -> sqlite3.Connection:
    # Create the gateway cache tables before adding synchronization metadata.
    cache = GatewayCache(s.database_path)
    cache.close()
    db = sqlite3.connect(s.database_path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sync_synced_rows (
            kind TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL,
            remote_object TEXT NOT NULL,
            PRIMARY KEY (kind, namespace, key)
        );
        CREATE TABLE IF NOT EXISTS sync_imported_objects (
            object_key TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_outbox (
            object_key TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            file_name TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_outbox_rows (
            kind TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL,
            object_key TEXT NOT NULL,
            PRIMARY KEY (kind, namespace, key)
        );
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            kind TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL,
            remote_object TEXT NOT NULL, local_sha256 TEXT NOT NULL,
            incoming_sha256 TEXT NOT NULL, detected_at TEXT NOT NULL,
            PRIMARY KEY (kind, namespace, key, remote_object)
        );
        CREATE TABLE IF NOT EXISTS sync_metadata (
            name TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    """)

    # A new destination cannot reuse rows marked as synced to the old one.
    scope = _json(
        {
            "endpoint": s.endpoint,
            "bucket": s.bucket,
            "compatibility_id": s.compatibility_id,
            "student_id": s.student_id,
        }
    )
    previous = db.execute(
        "SELECT value FROM sync_metadata WHERE name='destination'"
    ).fetchone()
    if previous is not None and previous["value"] != scope:
        db.close()
        raise RuntimeError(
            "The cache sync destination/identity has changed. "
            "Use the original B2 settings or a new local cache database "
            "before syncing a different group."
        )
    if previous is None:
        with db:
            db.execute(
                "INSERT INTO sync_metadata(name, value) VALUES ('destination', ?)",
                (scope,),
            )
    return db


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    if not columns or "namespace" not in columns or "key" not in columns:
        raise RuntimeError(f"Missing or invalid gateway cache table: {table}")
    return columns


def _schema(db: sqlite3.Connection) -> dict[str, list[str]]:
    return {kind: _columns(db, table) for kind, table in TABLES.items()}


def _pending_query(kind: str) -> str:
    table = TABLES[kind]
    return f"""
        FROM {table} c
        WHERE NOT EXISTS (
            SELECT 1 FROM sync_synced_rows x
            WHERE x.kind=? AND x.namespace=c.namespace AND x.key=c.key
        )
        AND NOT EXISTS (
            SELECT 1 FROM sync_outbox_rows x
            WHERE x.kind=? AND x.namespace=c.namespace AND x.key=c.key
        )
        AND NOT EXISTS (
            SELECT 1 FROM sync_conflicts x
            WHERE x.kind=? AND x.namespace=c.namespace AND x.key=c.key
        )
    """


def _pending_count(db: sqlite3.Connection, kind: str) -> int:
    return db.execute(
        "SELECT COUNT(*) " + _pending_query(kind), (kind, kind, kind)
    ).fetchone()[0]


def _next_rows(
    db: sqlite3.Connection,
    max_count: int,
) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for kind in TABLES:
        available = max_count - len(rows)
        if available <= 0:
            break
        # SQLite uses signed 64-bit limits; -1 allows larger configured batches.
        sqlite_limit = available if available <= (2**63 - 1) else -1
        cursor = db.execute(
            "SELECT c.* "
            + _pending_query(kind)
            + " ORDER BY c.namespace, c.key LIMIT ?",
            (kind, kind, kind, sqlite_limit),
        )
        rows.extend((kind, dict(row)) for row in cursor)
    return rows


def _write_archive(
    path: Path,
    s: SyncSettings,
    db: sqlite3.Connection,
    object_key: str,
    entries: list[tuple[str, dict[str, Any]]],
) -> None:
    header = {
        "type": "header",
        "format": FORMAT,
        "producer": s.student_id,
        "compatibility_id": s.compatibility_id,
        "object_key": object_key,
        "created_at": _utc_now(),
        "columns": _schema(db),
    }
    digest = hashlib.sha256()
    with path.open("wb") as output:
        with gzip.GzipFile(
            fileobj=output, mode="wb", filename="", mtime=0, compresslevel=6
        ) as z:
            z.write((_json(header) + "\n").encode("utf-8"))
            for kind, data in entries:
                line = (
                    _json({"type": "entry", "kind": kind, "row": data}) + "\n"
                ).encode("utf-8")
                digest.update(line)
                z.write(line)
            z.write(
                (
                    _json(
                        {
                            "type": "footer",
                            "count": len(entries),
                            "sha256": digest.hexdigest(),
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        output.flush()
        os.fsync(output.fileno())


def _prepare_outbox(s: SyncSettings, db: sqlite3.Connection) -> bool:
    # Save pending rows locally before recording them as queued for upload.
    choices = _next_rows(db, s.max_entries_per_archive)
    if not choices:
        return False

    uid = uuid.uuid4().hex
    key = f"cache/v1/{s.compatibility_id}/{s.student_id}/{uid}.jsonl.gz"
    outbox = s.state_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    destination = outbox / f"{uid}.jsonl.gz"

    with tempfile.NamedTemporaryFile(dir=outbox, suffix=".tmp", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        # Shrink the batch to its target size; a single row cannot be split.
        while True:
            _write_archive(temporary, s, db, key, choices)
            size = temporary.stat().st_size
            if size <= s.max_archive_bytes or len(choices) == 1:
                break
            choices = choices[: max(1, len(choices) // 2)]

        if size > s.max_archive_bytes:
            print(
                f"NOTICE: one entry requires {size / MIB:.2f} MiB, "
                f"above configured target {s.max_archive_bytes / MIB:g} MiB; "
                "publishing it alone."
            )

        digest = _sha_file(temporary)
        os.replace(temporary, destination)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO sync_outbox VALUES (?, ?, ?, ?)",
                (key, digest, destination.name, _utc_now()),
            )
            db.executemany(
                "INSERT INTO sync_outbox_rows VALUES (?, ?, ?, ?)",
                ((kind, row["namespace"], row["key"], key) for kind, row in choices),
            )
            db.commit()
        except Exception:
            db.rollback()
            destination.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _head(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _finish_outbox(
    s: SyncSettings,
    db: sqlite3.Connection,
    key: str,
    filename: str,
    digest: str,
    s3: Any,
) -> None:
    # Upload or verify the archive, then mark its rows as synchronized.
    path = s.state_dir / "outbox" / filename
    info = _head(s3, s.bucket, key)
    if info is None:
        if not path.is_file():
            raise RuntimeError(
                f"Pending archive is missing locally and remotely: {path}"
            )
        if _sha_file(path) != digest:
            raise RuntimeError(f"Pending local archive was modified: {path}")

        # boto3 handles multipart upload for large archives.
        s3.upload_file(
            Filename=str(path),
            Bucket=s.bucket,
            Key=key,
            ExtraArgs={
                "ContentType": "application/gzip",
                "Metadata": {"sha256": digest, "format": FORMAT},
            },
            Config=_transfer_config(),
        )
        info = _head(s3, s.bucket, key)

    if info is None or info.get("Metadata", {}).get("sha256") != digest:
        raise RuntimeError(f"Remote archive SHA-256 metadata mismatch: {key}")
    if path.is_file() and info.get("ContentLength") != path.stat().st_size:
        raise RuntimeError(f"Remote archive size mismatch: {key}")

    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            "SELECT kind, namespace, key FROM sync_outbox_rows WHERE object_key=?",
            (key,),
        ).fetchall()
        db.executemany(
            "INSERT OR IGNORE INTO sync_synced_rows VALUES (?, ?, ?, ?)",
            ((r["kind"], r["namespace"], r["key"], key) for r in rows),
        )
        db.execute(
            "INSERT OR IGNORE INTO sync_imported_objects VALUES (?, ?, ?)",
            (key, digest, _utc_now()),
        )
        db.execute("DELETE FROM sync_outbox_rows WHERE object_key=?", (key,))
        db.execute("DELETE FROM sync_outbox WHERE object_key=?", (key,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    path.unlink(missing_ok=True)


def push(s: SyncSettings, db: sqlite3.Connection, s3: Any) -> None:
    completed = 0
    while True:
        pending = db.execute(
            "SELECT object_key, sha256, file_name FROM sync_outbox "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if pending is None:
            if not _prepare_outbox(s, db):
                break
            continue
        _finish_outbox(
            s,
            db,
            pending["object_key"],
            pending["file_name"],
            pending["sha256"],
            s3,
        )
        completed += 1
        print(f"Published {pending['object_key']}")
    print(f"Push complete: {completed} archive(s).")


def _list_objects(s: SyncSettings, s3: Any) -> Iterator[tuple[str, int]]:
    prefix = f"cache/v1/{s.compatibility_id}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith(".jsonl.gz"):
                yield key, item["Size"]


def _import_archive(
    db: sqlite3.Connection,
    s: SyncSettings,
    key: str,
    sha256: str,
    archive: Path,
) -> tuple[int, int, int]:
    # Validate the full archive inside one transaction; keep conflicting local rows.
    inserted = matched = conflicts = 0
    schema = _schema(db)
    digest = hashlib.sha256()
    seen: set[tuple[str, str, str]] = set()
    count = 0

    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT sha256 FROM sync_imported_objects WHERE object_key=?", (key,)
        ).fetchone()
        if existing is not None:
            if existing["sha256"] != sha256:
                raise RuntimeError(f"Remote object changed since import: {key}")
            db.commit()
            return 0, 0, 0

        with gzip.open(archive, "rb") as z:
            first = z.readline()
            if not first:
                raise ValueError("Empty cache archive")
            header = json.loads(first)
            if not isinstance(header, dict) or (
                header.get("type") != "header"
                or header.get("format") != FORMAT
                or header.get("compatibility_id") != s.compatibility_id
                or header.get("object_key") != key
            ):
                raise ValueError("Cache archive header/version mismatch")
            if not isinstance(header.get("columns"), dict) or {
                kind: set(cols) for kind, cols in header["columns"].items()
            } != {kind: set(cols) for kind, cols in schema.items()}:
                raise ValueError(
                    "Cache table schemas differ; use the same gateway version"
                )
            producer = _safe_id(header.get("producer"), "producer")
            if not key.startswith(f"cache/v1/{s.compatibility_id}/{producer}/"):
                raise ValueError("Archive producer does not match its remote path")

            for line in z:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise TypeError("Invalid JSONL object")
                if obj.get("type") == "footer":
                    if (
                        obj.get("count") != count
                        or obj.get("sha256") != digest.hexdigest()
                    ):
                        raise ValueError("Archive entry count or SHA-256 mismatch")
                    if z.readline():
                        raise ValueError("Unexpected bytes after cache archive footer")
                    break

                kind = obj.get("kind")
                row = obj.get("row")
                if obj.get("type") != "entry" or kind not in TABLES:
                    raise ValueError("Unexpected record type in cache archive")
                if not isinstance(row, dict) or set(row) != set(schema[kind]):
                    raise ValueError(f"Unexpected columns in {kind} cache entry")
                namespace, cache_key = row.get("namespace"), row.get("key")
                if not isinstance(namespace, str) or not namespace:
                    raise ValueError("Invalid cache namespace")
                if not isinstance(cache_key, str) or not cache_key:
                    raise ValueError("Invalid cache key")
                identity = (kind, namespace, cache_key)
                if identity in seen:
                    raise ValueError(
                        f"Duplicate cache key within the archive: {identity}"
                    )
                seen.add(identity)

                table = TABLES[kind]
                current = db.execute(
                    f"SELECT * FROM {table} WHERE namespace=? AND key=?",
                    (namespace, cache_key),
                ).fetchone()
                if current is None:
                    names = list(row)
                    quoted = ",".join(
                        '"' + col.replace('"', '""') + '"' for col in names
                    )
                    marks = ",".join("?" for _ in names)
                    db.execute(
                        f"INSERT INTO {table} ({quoted}) VALUES ({marks})",
                        tuple(row[col] for col in names),
                    )
                    inserted += 1
                elif dict(current) == row:
                    matched += 1
                else:
                    conflicts += 1
                    db.execute(
                        "INSERT OR IGNORE INTO sync_conflicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            kind,
                            namespace,
                            cache_key,
                            key,
                            _sha_json(dict(current)),
                            _sha_json(row),
                            _utc_now(),
                        ),
                    )
                    digest.update(line)
                    count += 1
                    continue

                db.execute(
                    "INSERT OR IGNORE INTO sync_synced_rows VALUES (?, ?, ?, ?)",
                    (kind, namespace, cache_key, key),
                )
                digest.update(line)
                count += 1
            else:
                raise ValueError("Missing footer in cache archive")

        db.execute(
            "INSERT INTO sync_imported_objects VALUES (?, ?, ?)",
            (key, sha256, _utc_now()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return inserted, matched, conflicts


def pull(s: SyncSettings, db: sqlite3.Connection, s3: Any) -> None:
    inserted = matched = conflicts = archives = 0
    inbox = s.state_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    remote = sorted(_list_objects(s, s3))

    for key, expected_size in remote:
        if db.execute(
            "SELECT 1 FROM sync_imported_objects WHERE object_key=?", (key,)
        ).fetchone():
            continue

        info = _head(s3, s.bucket, key)
        if info is None:
            raise RuntimeError(f"Remote archive disappeared: {key}")
        advertised = info.get("Metadata", {}).get("sha256")
        if not isinstance(advertised, str) or not re.fullmatch(
            r"[0-9a-f]{64}", advertised
        ):
            raise ValueError(f"Remote archive is missing valid SHA-256 metadata: {key}")
        if info.get("ContentLength") != expected_size:
            raise RuntimeError(f"Remote archive changed while listing: {key}")

        with tempfile.NamedTemporaryFile(
            dir=inbox, suffix=".jsonl.gz", delete=False
        ) as tmp:
            local = Path(tmp.name)
        try:
            # boto3 streams large downloads; verify the completed file before import.
            s3.download_file(
                Bucket=s.bucket,
                Key=key,
                Filename=str(local),
                Config=_transfer_config(),
            )
            if local.stat().st_size != expected_size or _sha_file(local) != advertised:
                raise ValueError(f"Downloaded archive integrity check failed: {key}")
            added, same, different = _import_archive(db, s, key, advertised, local)
        finally:
            local.unlink(missing_ok=True)

        inserted += added
        matched += same
        conflicts += different
        archives += 1
        print(f"Imported {key}: {added} new, {same} duplicates, {different} conflicts")

    print(
        f"Pull complete: {archives} archive(s), {inserted} new entries, "
        f"{matched} duplicates, {conflicts} conflicts."
    )


def status(db: sqlite3.Connection) -> None:
    for kind, table in TABLES.items():
        total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        synced = db.execute(
            "SELECT COUNT(*) FROM sync_synced_rows WHERE kind=?", (kind,)
        ).fetchone()[0]
        queued = db.execute(
            "SELECT COUNT(*) FROM sync_outbox_rows WHERE kind=?", (kind,)
        ).fetchone()[0]
        print(
            f"{kind}: {total} local, {synced} synchronized, "
            f"{queued} queued, {_pending_count(db, kind)} new"
        )
    uploaded = db.execute("SELECT COUNT(*) FROM sync_imported_objects").fetchone()[0]
    outbox = db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
    conflicts = db.execute("SELECT COUNT(*) FROM sync_conflicts").fetchone()[0]
    print(
        f"Known archives: {uploaded}; pending uploads: {outbox}; conflicts: {conflicts}"
    )
    if conflicts:
        print("WARNING: conflicting cache rows were not overwritten")
        for row in db.execute(
            "SELECT kind, namespace, key, remote_object FROM sync_conflicts LIMIT 10"
        ):
            print(
                f"conflict {row['kind']} namespace={row['namespace']} "
                f"key={row['key']} source={row['remote_object']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backblaze B2 group-cache synchronization"
    )
    parser.add_argument("--config", type=Path, default=Path("gateway.toml"))
    parser.add_argument("action", choices=("status", "pull", "push"))
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(settings.state_dir / "sync.lock"), timeout=15):
            db = _open_db(settings)
            try:
                if args.action == "status":
                    status(db)
                else:
                    client = s3_client(settings)
                    if args.action == "pull":
                        pull(settings, db, client)
                    else:
                        push(settings, db, client)
                        status(db)
            finally:
                db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"cache-sync ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
