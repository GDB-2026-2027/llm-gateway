"""Diagnostic durations use the last upstream call and per-text token shares."""

import json
from importlib import import_module
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.config import GatewayConfig, ModelRoute
from llm_gateway.providers import AdapterResult, EmbeddingResult
from llm_gateway.server import create_app


class Clock:
    def __init__(self):
        self.now = 0.0

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def config(tmp_path):
    return GatewayConfig(
        host="127.0.0.1", port=0, database_path=tmp_path / "cache.sqlite3",
        log_path=tmp_path / "calls.jsonl", invalid_output_retries=1,
        http_error_retries=1, http_initial_delay_s=7, http_max_delay_s=7,
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


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("final_valid", [True, False])
def test_response_last_generation_is_logged_and_cached(tmp_path, monkeypatch, final_valid):
    clock = Clock()
    module = import_module("llm_gateway.responses")
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=lambda: 1_700_000_000.0, perf_counter=clock.perf_counter, sleep=clock.sleep,
    ))
    calls = 0

    def fake_chat(payload, route, *, max_output_tokens):
        nonlocal calls
        calls += 1
        clock.now += 2 if calls == 1 else 3
        return AdapterResult(
            text='{"answer":true}' if calls == 2 and final_valid else "invalid",
            tokens_in=5, tokens_out=2, latency_s=99,
            provider_response={"attempt": calls},
        )

    monkeypatch.setattr(module, "call_openai_compatible", fake_chat)
    payload = {
        "model": "fake/chat", "instructions": "Return JSON",
        "input": [{"role": "user", "content": "test"}],
        "text": {"format": {"type": "json_schema", "name": "answer", "strict": True,
                            "schema": {"type": "object", "properties": {"answer": {"type": "boolean"}},
                                       "required": ["answer"], "additionalProperties": False}}},
        "metadata": {"run_id": "r", "cache_namespace": "r"},
    }
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.post("/v1/responses", json=payload).status_code == 200
        if final_valid:
            assert client.post("/v1/responses", json=payload).status_code == 200
    assert calls == 2
    assert [(r["cache_hit"], r["latency_s"], r["parse_ok"]) for r in records(tmp_path)] == (
        [(False, 3.0, True), (True, 3.0, True)] if final_valid else [(False, 3.0, False)]
    )


def test_embedding_last_upstream_batch_is_logged_per_text_and_cached(tmp_path, monkeypatch):
    clock = Clock()
    module = import_module("llm_gateway.embeddings")
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=lambda: 1_700_000_000.0, perf_counter=clock.perf_counter, sleep=clock.sleep,
    ))
    calls = 0

    def fake_embed(texts, route):
        nonlocal calls
        calls += 1
        clock.now += 4 if calls == 1 else 5
        if calls == 1:
            request = httpx.Request("POST", "http://fake.test/v1/embeddings")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return EmbeddingResult(
            vectors=[[1.0, 2.0] for _ in texts], tokens_in=4,
            latency_s=99, tokens_in_by_text=[1, 3],
        )

    monkeypatch.setattr(module, "embed_openai_compatible", fake_embed)
    payload = {"model": "fake/embed", "input": ["one", "two"],
               "metadata": {"run_id": "r", "cache_namespace": "r"}}
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.post("/v1/embeddings", json=payload).status_code == 200
        assert client.post("/v1/embeddings", json=payload).status_code == 200
    assert calls == 2
    assert [(r["cache_hit"], r["latency_s"]) for r in records(tmp_path)] == [
        (False, 1.25), (False, 3.75), (True, 1.25), (True, 3.75),
    ]


@pytest.mark.parametrize(
    ("total_tokens", "per_text_tokens"), [(None, None), (0, [0, 0])]
)
def test_embedding_duration_without_token_weights_is_split_evenly(
    tmp_path, monkeypatch, total_tokens, per_text_tokens
):
    clock = Clock()
    module = import_module("llm_gateway.embeddings")
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=lambda: 1_700_000_000.0, perf_counter=clock.perf_counter, sleep=clock.sleep,
    ))

    def fake_embed(texts, route):
        clock.now += 5
        return EmbeddingResult(
            vectors=[[1.0, 2.0] for _ in texts], tokens_in=total_tokens,
            latency_s=99, tokens_in_by_text=per_text_tokens,
        )

    monkeypatch.setattr(module, "embed_openai_compatible", fake_embed)
    with TestClient(create_app(config(tmp_path))) as client:
        response = client.post("/v1/embeddings", json={
            "model": "fake/embed", "input": ["one", "two"],
            "metadata": {"run_id": "r", "cache_namespace": "r"},
        })
    assert response.status_code == 200
    assert [r["latency_s"] for r in records(tmp_path)] == [2.5, 2.5]
