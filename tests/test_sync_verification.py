"""Exercise the real cache-sync algorithm with an in-memory S3 stand-in."""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from llm_gateway.api_models import ResponsesRequest
from llm_gateway.cache import GatewayCache, ResponseCacheEntry
from llm_gateway.cache_sync import SyncSettings, _open_db, pull, push
from llm_gateway.config import GatewayConfig, ModelRoute
from llm_gateway.responses import request_cache_key
from llm_gateway.server import create_app


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, *, Bucket, Key):
        obj = self.objects.get((Bucket, Key))
        if obj is None:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(obj[0]), "Metadata": obj[1]}

    def upload_file(self, *, Filename, Bucket, Key, ExtraArgs, Config):
        assert (Bucket, Key) not in self.objects
        data = Path(Filename).read_bytes()
        assert hashlib.sha256(data).hexdigest() == ExtraArgs["Metadata"]["sha256"]
        self.objects[Bucket, Key] = data, ExtraArgs["Metadata"]

    def download_file(self, *, Bucket, Key, Filename, Config):
        Path(Filename).write_bytes(self.objects[Bucket, Key][0])

    def get_paginator(self, operation_name):
        assert operation_name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        yield {"Contents": [
            {"Key": key, "Size": len(data)}
            for (bucket, key), (data, _) in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]}


def settings(root: Path, student: str):
    return SyncSettings(
        database_path=root / student / "cache.sqlite3",
        state_dir=root / student / "state",
        bucket="fake-group-bucket",
        endpoint="https://s3.eu-central-003.backblazeb2.com",
        student_id=student,
        compatibility_id="phase3",
        access_key_id="fake",
        secret_access_key="fake",
    )


ROUTE = ModelRoute(
    backend="openai_compatible", upstream_model="simulated-chat",
    base_url="http://127.0.0.1:1/v1", json_schema_mode="native",
)


def payload(prompt: str):
    return {
        "model": "verify/model", "instructions": "Return JSON",
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": "answer", "strict": True,
                            "schema": {"type": "object", "properties": {"answer": {"type": "boolean"}},
                                       "required": ["answer"], "additionalProperties": False}}},
        "metadata": {"run_id": "sync-phase3", "cache_namespace": "group"},
    }


def key(prompt: str):
    return request_cache_key(ResponsesRequest.model_validate(payload(prompt)), ROUTE, 512)


def add(cache: GatewayCache, prompt: str, answer: bool, *, attempts: int = 1):
    cache.put_response(ResponseCacheEntry(
        namespace="group", key=key(prompt), text=f'{{"answer":{str(answer).lower()}}}',
        tokens_in=5, tokens_out=2, tokens_in_final=5, tokens_out_final=2,
        usage_complete=attempts == 1, latency_s=0.01, attempts=attempts, parse_retries=0,
        provider_response={"id": f"fake-{prompt}-{answer}"},
    ))


def test_two_students_and_same_key_conflict(tmp_path):
    remote = MemoryS3()
    a, b = settings(tmp_path, "student-a"), settings(tmp_path, "student-b")
    ca, cb = GatewayCache(a.database_path), GatewayCache(b.database_path)
    try:
        add(ca, "first", True, attempts=2)
        with _open_db(a) as db:
            push(a, db, remote)
        with _open_db(b) as db:
            pull(b, db, remote)
        assert cb.get_response("group", key("first")).text == '{"answer":true}'
        with TestClient(create_app(GatewayConfig(
            host="127.0.0.1", port=0, database_path=b.database_path,
            log_path=tmp_path / "b-calls.jsonl",
            models={"verify/model": ROUTE},
        ))) as client:
            reused = client.post("/v1/responses", json=payload("first"))
            assert reused.status_code == 200
            assert reused.json()["parse_ok"] is True
            assert reused.json()["output"][0]["content"][0]["text"] == '{"answer":true}'
        copied = json.loads((tmp_path / "b-calls.jsonl").read_text().splitlines()[0])
        assert (copied["cache_hit"], copied["attempts"], copied["other_retries"]) == (True, 2, 1)

        add(cb, "second", False)
        with _open_db(b) as db:
            push(b, db, remote)
        with _open_db(a) as db:
            pull(a, db, remote)
        assert {ca.get_response("group", key(p)).text for p in ("first", "second")} == {
            '{"answer":true}', '{"answer":false}'
        }
        assert len(remote.objects) == 2

        # Concurrent generations with the same canonical key but different values.
        add(ca, "collision", True)
        add(cb, "collision", False)
        with _open_db(a) as db:
            push(a, db, remote)
        with _open_db(b) as db:
            push(b, db, remote)
        assert len(remote.objects) == 4
        with _open_db(a) as db:
            pull(a, db, remote)
            assert db.execute("SELECT COUNT(*) FROM sync_conflicts").fetchone()[0] == 1
        with _open_db(b) as db:
            pull(b, db, remote)
            assert db.execute("SELECT COUNT(*) FROM sync_conflicts").fetchone()[0] == 1
        assert ca.get_response("group", key("collision")).text == '{"answer":true}'
        assert cb.get_response("group", key("collision")).text == '{"answer":false}'
        with _open_db(a) as db:
            push(a, db, remote)
        with _open_db(b) as db:
            push(b, db, remote)
        assert len(remote.objects) == 4
    finally:
        ca.close()
        cb.close()
