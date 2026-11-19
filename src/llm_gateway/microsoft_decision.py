"""Microsoft-Decision-1 adapter for the existing /v1/responses gateway.

Microsoft Foundry System One contract:
  POST {resource_base_url}/providers/microsoft/v1/systemone
  Header: api-key: <key>
  JSON: {model: <deployment>, state: ..., questions: {semops: ...}}

Only single noul/choice questions are supported. The full provider response,
including probabilities, is retained by the existing response-cache mechanism.
"""

from __future__ import annotations

import json
import os
import time
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import httpx

from .api_models import ResponsesRequest
from .config import ModelRoute
from .providers import AdapterResult


class DecisionRequestError(ValueError):
    """The gateway request cannot be represented by System One (HTTP 422)."""


QUESTION_ID = "semops"
# Boolean output uses P(true) >= 0.5; the raw probability remains in provider_response.
DEFAULT_THRESHOLD = 0.5
MAX_CHOICE_LABELS = 255


def decision_kind(schema: dict[str, Any]) -> tuple[str, list[str] | None]:
    """Accept only schemas we can implement faithfully without free-form text."""
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise DecisionRequestError("Decision model requires an object JSON Schema")
    if set(schema) - {"type", "properties", "required", "additionalProperties", "title", "description"}:
        raise DecisionRequestError("Decision model does not support these root schema constraints")
    if schema.get("additionalProperties", False) not in (False,):
        raise DecisionRequestError("Decision model requires additionalProperties=false or omitted")
    properties = schema["properties"]
    required = schema.get("required", [])
    if len(properties) != 1 or not isinstance(required, list) or set(required) != set(properties) or len(required) != 1:
        raise DecisionRequestError("Decision schema must contain one required property")
    key, prop = next(iter(properties.items()))
    if not isinstance(prop, dict):
        raise DecisionRequestError("Invalid property definition")
    if key == "answer" and prop.get("type") == "boolean":
        if set(prop) - {"type", "title", "description"}:
            raise DecisionRequestError("Unsupported Boolean schema constraints")
        return "noul", None
    if key == "label" and prop.get("type") == "string":
        if set(prop) - {"type", "enum", "title", "description"}:
            raise DecisionRequestError("Unsupported classification schema constraints")
        labels = prop.get("enum")
        if not isinstance(labels, list) or not 2 <= len(labels) <= MAX_CHOICE_LABELS:
            raise DecisionRequestError("Classification requires 2 to 255 labels")
        if any(not isinstance(l, str) or not l.strip() or len(l) > 128 for l in labels):
            raise DecisionRequestError("Labels must be nonempty strings of at most 128 characters")
        if len(set(labels)) != len(labels):
            raise DecisionRequestError("Classification labels must be distinct")
        return "choice", labels
    raise DecisionRequestError("Only {answer: boolean} and {label: enum[string]} are supported")


def validate_decision_request(payload: ResponsesRequest, route: ModelRoute) -> None:
    """Fail fast before cache lookup and provider calls for unsupported options."""
    if route.backend != "microsoft_decision":
        raise DecisionRequestError("Incorrect backend")
    if payload.temperature is not None:
        raise DecisionRequestError("System One does not support temperature")
    if payload.top_logprobs not in (None, 0):
        raise DecisionRequestError("System One does not expose token logprobs")
    decision_kind(payload.text.format.schema_definition)


def build_decision_body(payload: ResponsesRequest, route: ModelRoute) -> dict[str, Any]:
    validate_decision_request(payload, route)
    kind, labels = decision_kind(payload.text.format.schema_definition)
    # Send the full context; System One answers a typed question, not JSON.
    state: dict[str, Any] = {
        "instructions": payload.instructions,
        "messages": [m.model_dump() for m in payload.input],
    }
    if kind == "noul":
        question: dict[str, Any] = {
            "type": "noul",
            "instructions": (
                "Using the state.instructions and state.messages, determine whether "
                "the correct response to the requested Boolean question or predicate "
                "would have answer=true."
            ),
        }
    else:
        assert labels is not None
        question = {
            "type": "choice",
            "instructions": (
                "Using the state.instructions and state.messages, choose the single "
                "best label for the requested classification."
            ),
            "criteria": {label: label for label in labels},
        }
    return {"model": route.upstream_model, "state": state,
            "questions": {QUESTION_ID: question}}


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"Invalid {name}: expected a finite number")
    if not (0 <= value <= 1):
        raise ValueError(f"Invalid {name}: must lie between 0 and 1")
    return float(value)


def interpret_decision_response(data: dict[str, Any], kind: str,
                                labels: list[str] | None) -> str:
    """Validate the provider's typed answer and translate it to SemOps JSON."""
    answers = data.get("answers")
    if not isinstance(answers, dict) or QUESTION_ID not in answers:
        raise ValueError("System One response is missing answers.semops")
    answer = answers[QUESTION_ID]
    if not isinstance(answer, dict) or answer.get("type") != kind:
        raise ValueError("System One returned an unexpected decision type")
    if kind == "noul":
        p = _probability(answer.get("noul"), "noul")
        return json.dumps({"answer": p >= DEFAULT_THRESHOLD}, separators=(",", ":"))
    assert labels is not None
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in labels:
        raise ValueError("System One selected a label outside the requested enum")
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        # Accept both probability formats; leave the cached provider payload unchanged.
        if isinstance(probabilities, list):
            parsed_probs: dict[str, Any] = {}
            for item in probabilities:
                if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                    raise ValueError("Invalid System One probability entry")
                label = item["value"]
                if label in parsed_probs:
                    raise ValueError("Duplicate probability label")
                parsed_probs[label] = item.get("probability")
        elif isinstance(probabilities, dict):
            parsed_probs = probabilities
        else:
            raise ValueError("Invalid System One probability distribution")
        if set(parsed_probs) != set(labels):
            raise ValueError("System One returned probabilities for unexpected labels")
        for label, p in parsed_probs.items():
            _probability(p, f"probabilities[{label!r}]")
    if "confidence" in answer:
        _probability(answer["confidence"], "confidence")
    return json.dumps({"label": choice}, ensure_ascii=False, separators=(",", ":"))


def _token_count(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0 or int(value) != value:
        raise ValueError(f"Invalid {label} returned by System One")
    return int(value)


def _resource_url(route: ModelRoute) -> str:
    if not route.base_url:
        raise ValueError("System One resource endpoint is missing")
    parsed = urlsplit(route.base_url)
    is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (parsed.scheme != "https" and not (is_local and parsed.scheme == "http")) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("System One endpoint must use HTTPS and contain no credentials or query")
    if parsed.path not in ("", "/"):
        raise ValueError("System One base_url must be the RESOURCE endpoint root")
    return route.base_url.rstrip("/") + "/providers/microsoft/v1/systemone"


def call_microsoft_decision(
    payload: ResponsesRequest,
    route: ModelRoute,
    client: httpx.Client | None = None,
) -> AdapterResult:
    """Invoke Azure using the api-key header; never log the key or body."""
    body = build_decision_body(payload, route)
    kind, labels = decision_kind(payload.text.format.schema_definition)
    url = _resource_url(route)
    if not route.api_key_env:
        raise ValueError("System One api_key_env not configured")
    secret = os.environ.get(route.api_key_env)
    if not secret:
        raise ValueError(f"Missing System One credential variable {route.api_key_env}")
    headers = {"api-key": secret, "Content-Type": "application/json", "Accept": "application/json"}
    started = time.perf_counter()
    if client is None:
        with httpx.Client(timeout=120.0) as session:
            response = session.post(url, json=body, headers=headers)
    else:
        response = client.post(url, json=body, headers=headers, timeout=120.0)
    latency = time.perf_counter() - started
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("System One returned a non-object response")
    text = interpret_decision_response(data, kind, labels)
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("Invalid System One usage")
    usage = usage or {}
    # Missing token counts stay unknown; do not estimate usage for pricing.
    tokens_in = _token_count(usage.get("input_tokens"), "input_tokens")
    tokens_out = _token_count(usage.get("output_tokens"), "output_tokens")
    return AdapterResult(text=text, tokens_in=tokens_in, tokens_out=tokens_out,
                         latency_s=latency, provider_response=data)


def noul_probability(gateway_result: dict[str, Any]) -> float:
    """Read P(true) from the existing gateway envelope, including on cache hits."""
    return _probability(
        gateway_result["provider_response"]["answers"][QUESTION_ID]["noul"], "noul"
    )
