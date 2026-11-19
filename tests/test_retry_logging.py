"""Verify per-invocation retry counters and terminal token fields via HTTP."""

import json
from collections import defaultdict
from importlib import import_module

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.config import GatewayConfig, ModelRoute
from llm_gateway.providers import AdapterResult, EmbeddingResult
from llm_gateway.server import create_app


SCHEMA = {
    "type": "object", "properties": {"answer": {"type": "boolean"}},
    "required": ["answer"], "additionalProperties": False,
}


def request(prompt, run_id="run"):
    return {
        "model": "fake/chat", "instructions": "Return JSON",
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": "answer", "strict": True, "schema": SCHEMA}},
        "metadata": {"run_id": run_id, "cache_namespace": run_id},
    }


def config(tmp_path):
    return GatewayConfig(
        host="127.0.0.1", port=0, database_path=tmp_path / "cache.sqlite3",
        log_path=tmp_path / "calls.jsonl", invalid_output_retries=2,
        http_error_retries=1, http_initial_delay_s=0, http_max_delay_s=0,
        models={
            "fake/chat": ModelRoute(
                backend="openai_compatible", upstream_model="fake",
                base_url="http://127.0.0.1:1/v1", json_schema_mode="native",
            ),
            "fake/embed": ModelRoute(
                backend="openai_compatible", upstream_model="fake-embed",
                base_url="http://127.0.0.1:1/v1", task="embeddings",
            ),
        },
    )


def too_many_requests():
    request = httpx.Request("POST", "http://fake.test/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.parametrize(
    ("steps", "parse_retries", "other_retries", "parse_ok", "aggregate_tokens"),
    [
        (["valid"], 0, 0, True, 5),
        (["invalid", "valid"], 1, 0, True, 10),
        (["invalid", "invalid", "valid"], 2, 0, True, 15),
        (["429", "valid"], 0, 1, True, 5),
        (["invalid", "429", "valid"], 1, 1, True, 10),
        (["invalid", "invalid", "invalid"], 2, 0, False, 15),
    ],
)
def test_response_retry_log(
    tmp_path, monkeypatch, steps, parse_retries, other_retries, parse_ok, aggregate_tokens
):
    pending = iter(steps)

    def fake_chat(payload, route, *, max_output_tokens):
        step = next(pending)
        if step == "429":
            raise too_many_requests()
        return AdapterResult(
            text='{"answer":true}' if step == "valid" else "not JSON",
            tokens_in=5, tokens_out=2, latency_s=0.01,
            provider_response={"step": step},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(config(tmp_path))) as client:
        response = client.post("/v1/responses", json=request("test"))
    assert response.status_code == 200
    assert response.json()["parse_ok"] is parse_ok
    record = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[0])
    assert record["attempts"] == len(steps)
    assert record["parse_retries"] == parse_retries
    assert record["other_retries"] == other_retries
    assert record["tokens_in"] == aggregate_tokens
    assert record["tokens_out"] == aggregate_tokens // 5 * 2
    assert (record["tokens_in_last"], record["tokens_out_last"]) == (5, 2)
    assert (record["tokens_in_final"], record["tokens_out_final"]) == (
        (5, 2) if parse_ok else (None, None)
    )


def test_cache_hit_repeats_old_parse_count_but_no_new_retry(tmp_path, monkeypatch):
    calls = 0

    def fake_chat(payload, route, *, max_output_tokens):
        nonlocal calls
        calls += 1
        return AdapterResult(
            text="not JSON" if calls == 1 else '{"answer":true}',
            tokens_in=5, tokens_out=2, latency_s=0.01,
            provider_response={"call": calls},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.post("/v1/responses", json=request("cache")).status_code == 200
        assert client.post("/v1/responses", json=request("cache")).status_code == 200
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls == 2
    assert [(r["cache_hit"], r["parse_retries"], r["other_retries"]) for r in records] == [
        (False, 1, 0), (True, 1, 0)
    ]


@pytest.mark.parametrize(
    ("steps", "parse_retries", "other_retries"),
    [
        (["429", "valid"], 0, 1),
        (["invalid", "429", "valid"], 1, 1),
    ],
)
def test_response_cache_hit_copies_other_retries(
    tmp_path, monkeypatch, steps, parse_retries, other_retries
):
    pending = iter(steps)
    calls = 0

    def fake_chat(payload, route, *, max_output_tokens):
        nonlocal calls
        calls += 1
        step = next(pending)
        if step == "429":
            raise too_many_requests()
        return AdapterResult(
            text='{"answer":true}' if step == "valid" else "not JSON",
            tokens_in=5, tokens_out=2, latency_s=0.01,
            provider_response={"step": step},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(config(tmp_path))) as client:
        for _ in range(2):
            assert client.post("/v1/responses", json=request("copied-retries")).status_code == 200
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls == len(steps)
    assert [(r["cache_hit"], r["attempts"], r["parse_retries"], r["other_retries"])
            for r in records] == [
                (False, len(steps), parse_retries, other_retries),
                (True, len(steps), parse_retries, other_retries),
            ]


def test_embedding_http_retry_is_copied_on_reuse(tmp_path, monkeypatch):
    calls = 0

    def fake_embed(texts, route):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise too_many_requests()
        return EmbeddingResult(
            vectors=[[1.0, 2.0] for _ in texts], tokens_in=3 * len(texts),
            latency_s=0.01, tokens_in_by_text=[3] * len(texts),
        )

    monkeypatch.setattr(import_module("llm_gateway.embeddings"), "embed_openai_compatible", fake_embed)
    with TestClient(create_app(config(tmp_path))) as client:
        for _ in range(2):
            result = client.post("/v1/embeddings", json={
                "model": "fake/embed", "input": ["same", "same"],
                "metadata": {"run_id": "embed-run", "cache_namespace": "embed-run"},
            })
            assert result.status_code == 200
    assert result.status_code == 200 and calls == 2
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [(r["cache_hit"], r["other_retries"]) for r in records] == [
        (False, 1), (True, 1), (True, 1), (True, 1)
    ]
    assert sum(r["other_retries"] for r in records if not r["cache_hit"]) == 1


def test_multiple_keys_keep_invocations_separate(tmp_path, monkeypatch):
    steps = {"parse": ["invalid", "valid"], "http": ["429", "valid"], "plain": ["valid"]}
    positions = defaultdict(int)

    def fake_chat(payload, route, *, max_output_tokens):
        prompt = payload.input[-1].content
        index = positions[prompt]
        positions[prompt] += 1
        step = steps[prompt][index]
        if step == "429":
            raise too_many_requests()
        return AdapterResult(
            text='{"answer":true}' if step == "valid" else "not JSON",
            tokens_in=5, tokens_out=2, latency_s=0.01,
            provider_response={"step": step},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(config(tmp_path))) as client:
        for prompt in ("parse", "http", "plain", "parse"):
            assert client.post("/v1/responses", json=request(prompt, "many")).status_code == 200
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(records) == 4
    assert sum(r["parse_retries"] for r in records if not r["cache_hit"]) == 1
    assert sum(r["other_retries"] for r in records if not r["cache_hit"]) == 1
    assert records[-1]["cache_hit"] is True
