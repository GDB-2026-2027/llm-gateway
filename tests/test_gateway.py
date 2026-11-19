"""Local gateway smoke tests; no cloud credentials or running providers required."""

import json
import sqlite3
from importlib import import_module

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.config import load_config
from llm_gateway.providers import AdapterResult, EmbeddingResult
from llm_gateway.server import create_app


def make_config(tmp_path, *, include_cloud=False):
    models = tmp_path / "models.toml"
    models.write_text(
        '[models."local/chat"]\n'
        'backend = "openai_compatible"\n'
        'upstream_model = "chat-upstream"\n'
        'base_url = "http://127.0.0.1:11434/v1"\n'
        'json_schema_mode = "native"\n'
        '\n[models."local/embed"]\n'
        'backend = "openai_compatible"\n'
        'task = "embeddings"\n'
        'upstream_model = "embed-upstream"\n'
        'base_url = "http://127.0.0.1:11434/v1"\n'
        + (
            '\n[models."cloud/chat"]\n'
            'backend = "google_genai"\n'
            'upstream_model = "cloud-upstream"\n'
            'json_schema_mode = "native"\n'
            '\n[models."azure/chat"]\n'
            'backend = "openai_compatible"\n'
            'upstream_model = "azure-upstream"\n'
            'base_url = "https://example.invalid/openai/v1"\n'
            'api_key_env = "TEST_AZURE_MISSING_KEY"\n'
            'json_schema_mode = "native"\n'
            if include_cloud else ''
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 8000\n'
        '[cache]\ndatabase_path = "cache.sqlite3"\n'
        '[routing]\nmodels_path = "models.toml"\n'
        '[logging]\nlog_path = "calls.jsonl"\n',
        encoding="utf-8",
    )
    return load_config(config_file)


def response_request(model="local/chat"):
    return {
        "model": model,
        "instructions": "Return an answer",
        "input": [{"role": "user", "content": "Is this a test?"}],
        "text": {"format": {
            "type": "json_schema", "name": "boolean_answer", "strict": True,
            "schema": {"type": "object", "properties": {
                "answer": {"type": "boolean"}}, "required": ["answer"],
                "additionalProperties": False},
        }},
        "metadata": {"run_id": "run-1", "cache_namespace": "group-1"},
    }


def test_config_loads_local_models_without_google(tmp_path):
    config = make_config(tmp_path)
    assert config.google.project_id is None
    assert config.models["local/chat"].task == "responses"
    assert config.models["local/embed"].task == "embeddings"


def test_start_and_local_calls_with_unconfigured_cloud_route(tmp_path, monkeypatch):
    config = make_config(tmp_path, include_cloud=True)
    assert config.google.project_id is None
    assert "cloud/chat" in config.models

    calls = []

    def fake_chat(payload, route, *, max_output_tokens):
        calls.append((route.upstream_model, payload.model, max_output_tokens))
        return AdapterResult(
            text='{"answer":true}', tokens_in=7, tokens_out=3,
            latency_s=0.01, provider_response={"id": "simulated-chat"},
        )

    def fake_embed(texts, route):
        calls.append((route.upstream_model, list(texts)))
        return EmbeddingResult(
            vectors=[[1.0, 0.5] for _ in texts], tokens_in=len(texts) * 2,
            latency_s=0.01, tokens_in_by_text=[2 for _ in texts],
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    monkeypatch.setattr(import_module("llm_gateway.embeddings"), "embed_openai_compatible", fake_embed)

    with TestClient(create_app(config)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        assert client.post("/v1/responses", json=response_request("unknown")).status_code == 404
        assert client.post("/v1/embeddings", json={
            "model": "local/chat", "input": "text",
            "metadata": {"run_id": "run-1", "cache_namespace": "group-1"},
        }).status_code == 422

        first = client.post("/v1/responses", json=response_request())
        second = client.post("/v1/responses", json=response_request())
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["parse_ok"] is True
        assert first.json()["output"][0]["content"][0]["text"] == '{"answer":true}'
        assert calls == [("chat-upstream", "local/chat", 512)]

        embedding_payload = {
            "model": "local/embed", "input": ["one", "two", "one"],
            "metadata": {"run_id": "run-1", "cache_namespace": "group-1"},
        }
        embedded = client.post("/v1/embeddings", json=embedding_payload)
        cached = client.post("/v1/embeddings", json=embedding_payload)
        assert embedded.status_code == cached.status_code == 200
        assert embedded.json()["data"] == cached.json()["data"]
        assert [item["index"] for item in embedded.json()["data"]] == [0, 1, 2]
        assert embedded.json()["data"][0]["embedding"] == [1.0, 0.5]
        assert calls[-1] == ("embed-upstream", ["one", "two"])
        assert len(calls) == 2

    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    responses = [record for record in records if record["endpoint"] == "responses"]
    embeddings = [record for record in records if record["endpoint"] == "embeddings"]
    assert [record["cache_hit"] for record in responses] == [False, True]
    assert responses[0]["tokens_in"] == 7
    assert responses[0]["run_id"] == "run-1"
    assert len(embeddings) == 6
    assert sum(not record["cache_hit"] for record in embeddings) == 2


def test_cloud_configuration_is_checked_on_use(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_AZURE_MISSING_KEY", raising=False)
    config = make_config(tmp_path, include_cloud=True)
    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/responses", json=response_request("cloud/chat")).status_code == 502
        assert client.post("/v1/responses", json=response_request("azure/chat")).status_code == 502


@pytest.mark.parametrize("answer", [True, False])
def test_valid_boolean_and_cache_hit_keep_existing_envelope(tmp_path, monkeypatch, answer):
    calls = []

    def fake_chat(payload, route, *, max_output_tokens):
        calls.append(payload.model)
        return AdapterResult(
            text=json.dumps({"answer": answer}), tokens_in=10, tokens_out=4,
            latency_s=0.01, provider_response={"id": "valid-generation"},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(make_config(tmp_path))) as client:
        first = client.post("/v1/responses", json=response_request())
        second = client.post("/v1/responses", json=response_request())

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert set(first.json()) == {"output", "parse_ok", "provider_response"}
    assert first.json()["parse_ok"] is True
    assert first.json()["provider_response"] == {"id": "valid-generation"}
    assert first.json()["output"][0]["type"] == "message"
    content = first.json()["output"][0]["content"][0]
    assert content["type"] == "output_text"
    assert json.loads(content["text"]) == {"answer": answer}
    assert calls == ["local/chat"]

    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [record["cache_hit"] for record in records] == [False, True]
    assert [record["parse_ok"] for record in records] == [True, True]
    assert all(record["error_type"] is None for record in records)


def test_invalid_then_valid_generation_is_cached_with_retry_history(tmp_path, monkeypatch):
    outputs = iter(["not JSON", '{"answer":true}'])
    calls = []

    def fake_chat(payload, route, *, max_output_tokens):
        calls.append(payload.model)
        return AdapterResult(
            text=next(outputs), tokens_in=8, tokens_out=2,
            latency_s=0.01, provider_response={"id": f"attempt-{len(calls)}"},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(make_config(tmp_path))) as client:
        first = client.post("/v1/responses", json=response_request())
        cached = client.post("/v1/responses", json=response_request())

    assert first.status_code == cached.status_code == 200
    assert first.json() == cached.json()
    assert first.json()["parse_ok"] is True
    assert "error_type" not in first.json()
    assert len(calls) == 2
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [record["cache_hit"] for record in records] == [False, True]
    assert [record["parse_retries"] for record in records] == [1, 1]
    assert [record["attempts"] for record in records] == [2, 2]
    assert all(record["parse_ok"] is True and record["error_type"] is None for record in records)


def test_exhausted_parse_retries_return_uncached_boolean_fallback(tmp_path, monkeypatch):
    calls = []

    def fake_chat(payload, route, *, max_output_tokens):
        calls.append(payload.model)
        return AdapterResult(
            text="invalid output", tokens_in=6, tokens_out=2,
            latency_s=0.01, provider_response={"id": f"invalid-{len(calls)}"},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(make_config(tmp_path))) as client:
        first = client.post("/v1/responses", json=response_request())
        second = client.post("/v1/responses", json=response_request())

    assert first.status_code == second.status_code == 200
    assert len(calls) == 4  # A second request must retry upstream, not read a fallback from cache.
    for response in (first, second):
        body = response.json()
        assert set(body) == {"output", "parse_ok", "provider_response", "error_type"}
        assert body["parse_ok"] is False
        assert body["output"][0]["type"] == "message"
        assert body["error_type"] == "parse_error"
        assert body["output"][0]["content"][0]["type"] == "output_text"
        assert json.loads(body["output"][0]["content"][0]["text"]) == {"answer": False}

    with sqlite3.connect(tmp_path / "cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0] == 0
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert all(record["cache_hit"] is False for record in records)
    assert all(record["parse_ok"] is False for record in records)
    assert all(record["error_type"] == "parse_error" for record in records)
    assert all(record["attempts"] == 2 and record["parse_retries"] == 1 for record in records)


def test_invalid_classification_uses_reserved_label(tmp_path, monkeypatch):
    def fake_chat(payload, route, *, max_output_tokens):
        return AdapterResult(
            text='{"label":"invalid"}', tokens_in=5, tokens_out=2,
            latency_s=0.01, provider_response={"id": "invalid-classification"},
        )

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    request = response_request()
    request["text"]["format"]["schema"] = {
        "type": "object", "properties": {
            "label": {"type": "string", "enum": ["A", "__PARSE_FAILURE__"]}},
        "required": ["label"], "additionalProperties": False,
    }
    with TestClient(create_app(make_config(tmp_path))) as client:
        response = client.post("/v1/responses", json=request)

    assert response.status_code == 200
    assert response.json()["parse_ok"] is False
    assert response.json()["error_type"] == "parse_error"
    assert json.loads(response.json()["output"][0]["content"][0]["text"]) == {
        "label": "___PARSE_FAILURE__"
    }
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert records[0]["parse_ok"] is False
    assert records[0]["error_type"] == "parse_error"


def test_transport_error_is_not_reported_as_parse_error(tmp_path, monkeypatch):
    def fake_chat(payload, route, *, max_output_tokens):
        raise httpx.ConnectError("simulated transport failure")

    monkeypatch.setattr(import_module("llm_gateway.responses"), "call_openai_compatible", fake_chat)
    with TestClient(create_app(make_config(tmp_path))) as client:
        response = client.post("/v1/responses", json=response_request())

    assert response.status_code == 502
    assert response.json() == {"detail": "LLM backend request failed"}
    record = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[0])
    assert record["parse_ok"] is None
    assert record["error_type"] == "ConnectError"
