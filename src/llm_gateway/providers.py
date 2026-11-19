"""Calls to the configured chat and embedding providers."""

import json
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from numbers import Integral, Real
from typing import Any
from urllib.parse import urlsplit

import httpx
from google import genai
from google.genai import types

from .api_models import ResponsesRequest
from .config import GoogleConfig, ModelRoute

DEFAULT_MAX_OUTPUT_TOKENS = 512


def _auth_headers(route: ModelRoute) -> dict[str, str]:
    """Return the authorization headers for a given route."""

    if route.api_key_env is None:
        return {}

    parsed_url = urlsplit(route.base_url or "")

    is_local = parsed_url.hostname in ("localhost", "127.0.0.1", "::1")

    if not is_local and parsed_url.scheme != "https":
        raise ValueError(
            f"API key cannot be used with non-HTTPS URL '{route.base_url}' unless it's a local address."
        )

    api_key = os.getenv(route.api_key_env)

    if not api_key:
        raise ValueError(
            f"API key not found in environment variable '{route.api_key_env}'."
        )

    return {"Authorization": f"Bearer {api_key}"}


@dataclass(frozen=True)
class AdapterResult:
    text: str
    tokens_in: int | None
    tokens_out: int | None
    latency_s: float
    provider_response: dict[str, Any]


def call_openai_compatible(
    payload: ResponsesRequest,
    route: ModelRoute,
    client: httpx.Client | None = None,
    *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> AdapterResult:
    """Call an OpenAI-compatible chat completion backend."""

    if route.backend != "openai_compatible" or not route.base_url:
        raise ValueError("Invalid route configuration for OpenAI-compatible backend.")

    url = f"{route.base_url.rstrip('/')}/chat/completions"

    system_instructions = payload.instructions

    if route.json_schema_mode == "prompt":
        schema_json = json.dumps(
            payload.text.format.schema_definition, ensure_ascii=False, sort_keys=True
        )

        system_instructions += (
            "\n\nReturn ONLY valid JSON matching this JSON Schema."
            "\nDo not include Markdown, explanations or code fences."
            f"\nJSON Schema:\n{schema_json}"
        )

    messages = [
        {"role": "system", "content": system_instructions},
        *[message.model_dump() for message in payload.input],
    ]

    body = {
        "model": route.upstream_model,
        "messages": messages,
        "max_tokens": max_output_tokens,
        "stream": False,
    }

    if route.json_schema_mode == "native":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": payload.text.format.name,
                "strict": True,
                "schema": payload.text.format.schema_definition,
            },
        }

    elif route.json_schema_mode != "prompt":
        raise ValueError(
            f"Invalid json_schema_mode '{route.json_schema_mode}' for model '{route.upstream_model}'."
        )

    if payload.temperature is not None:
        body["temperature"] = payload.temperature

    # Request the logprob depth configured for this model.
    if route.top_logprobs is not None and route.top_logprobs > 0:
        body["logprobs"] = True
        body["top_logprobs"] = route.top_logprobs

    headers = _auth_headers(route)

    start_time = time.perf_counter()

    if client is None:
        with httpx.Client(timeout=120.0) as session:
            response = session.post(url, json=body, headers=headers)
    else:
        response = client.post(url, json=body, headers=headers, timeout=120.0)

    latency_s = time.perf_counter() - start_time

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise TypeError("Invalid response format from OpenAI-compatible backend.")

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            "Invalid response format from OpenAI-compatible backend."
        ) from e

    if not isinstance(text, str):
        raise TypeError(
            "Invalid response format from OpenAI-compatible backend: 'content' is not a string."
        )

    usage = data.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise TypeError("Invalid usage returned by backend.")

    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("completion_tokens")
    for count in (tokens_in, tokens_out):
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("Invalid token count returned by backend.")

    return AdapterResult(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_s=latency_s,
        provider_response=data,
    )


def call_google_genai(
    payload: ResponsesRequest,
    route: ModelRoute,
    google: GoogleConfig,
    *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> AdapterResult:
    """Call Google Gemini through Vertex AI"""

    if route.backend != "google_genai":
        raise ValueError("Invalid route configuration for Google GenAI backend.")

    if not google.project_id:
        raise ValueError("Google project_id is not configured.")

    # Gemini takes system messages as instructions and uses user/model turns.
    system_parts = [payload.instructions]
    contents = []

    for message in payload.input:
        if message.role == "system":
            system_parts.append(message.content)
        else:
            role = "model" if message.role == "assistant" else "user"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=message.content)],
                )
            )
    if not contents:
        raise ValueError("No user or assistant messages provided.")

    config_kwargs = {
        "max_output_tokens": max_output_tokens,
    }

    # Use provider-side schema enforcement when configured; otherwise prompt for JSON.
    if route.json_schema_mode == "native":
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_json_schema"] = payload.text.format.schema_definition
    elif route.json_schema_mode == "prompt":
        schema_json = json.dumps(
            payload.text.format.schema_definition, ensure_ascii=False, sort_keys=True
        )

        system_parts.append(
            "\n\nReturn ONLY valid JSON matching this JSON Schema."
            "\nDo not include Markdown, explanations or code fences."
            f"\nJSON Schema:\n{schema_json}"
        )

    else:
        raise ValueError("Invalid json_schema_mode.")

    config_kwargs["system_instruction"] = "\n\n".join(system_parts)

    if payload.temperature is not None:
        config_kwargs["temperature"] = payload.temperature

    if route.top_logprobs is not None and route.top_logprobs > 0:
        config_kwargs["response_logprobs"] = True
        config_kwargs["logprobs"] = route.top_logprobs

    generation_config = types.GenerateContentConfig(**config_kwargs)

    start_time = time.perf_counter()

    with genai.Client(
        vertexai=True,
        project=google.project_id,
        location=route.location or google.location or "global",
    ) as client:
        response = client.models.generate_content(
            model=route.upstream_model,
            contents=contents,
            config=generation_config,
        )

    latency_s = time.perf_counter() - start_time

    generated_text = response.text

    if generated_text is None:
        text = ""
    elif isinstance(generated_text, str):
        text = generated_text
    else:
        raise TypeError(
            "Invalid response format from Google GenAI backend: 'text' is not a string."
        )

    usage = response.usage_metadata

    tokens_in = None
    tokens_out = None

    # Thinking tokens contribute to output usage even when they are not visible.
    if usage is not None:
        tokens_in = usage.prompt_token_count
        visible_tokens = usage.candidates_token_count
        thinking_tokens = usage.thoughts_token_count

        if visible_tokens is not None or thinking_tokens is not None:
            tokens_out = (visible_tokens or 0) + (thinking_tokens or 0)

    provider_response = response.model_dump(mode="json", exclude_none=True)

    return AdapterResult(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_s=latency_s,
        provider_response=provider_response,
    )


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    tokens_in: int | None
    latency_s: float
    tokens_in_by_text: list[int] | None = None


def _validate_inputs(texts: list[str]) -> None:
    if (
        not isinstance(texts, list)
        or not texts
        or any(not isinstance(t, str) for t in texts)
    ):
        raise ValueError("Input must be a non-empty list of strings.")


def _validate_vectors(raw_vectors: list[Any], expected_count: int) -> list[list[float]]:
    if len(raw_vectors) != expected_count:
        raise ValueError(
            f"Expected {expected_count} vectors, but got {len(raw_vectors)}."
        )

    vectors = []
    dimension = None

    for raw_vector in raw_vectors:
        if isinstance(raw_vector, (str, bytes)):
            raise TypeError("Vector should be a list of floats, not a string or bytes.")

        try:
            values = list(raw_vector)

        except TypeError as e:
            raise ValueError("Invalid embedding vector") from e

        if not values:
            raise ValueError("Embedding vector cannot be empty.")

        if any(isinstance(v, bool) or not isinstance(v, Real) for v in values):
            raise TypeError("All vector elements must be numeric (int or float).")

        vector = [float(v) for v in values]

        if not all(isfinite(v) for v in vector):
            raise ValueError("All vector elements must be finite numbers.")

        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError("All embedding vectors must have the same dimension.")

        vectors.append(vector)

    return vectors


@lru_cache(maxsize=1)
def _load_nomic_tokenizer():
    # Use a local tokenizer when available; token allocation has a fallback.
    try:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        path = hf_hub_download(
            repo_id="google-bert/bert-base-uncased",
            filename="tokenizer.json",
            local_files_only=True,
        )

        return Tokenizer.from_file(path)

    except Exception:  # noqa: BLE001
        return None


def _apportion_embedding_tokens(
    texts: list[str], total_tokens: int, tokenizer: Any | None
) -> list[int]:
    weights = None
    if tokenizer is not None:
        try:
            weights = [max(1, len(tokenizer.encode(text).ids)) for text in texts]
        except Exception:  # noqa: BLE001, S110
            pass  # Use word counts below if tokenization fails.

    if weights is None:
        # Estimate each text's share from its word count.
        weights = [max(1, len(text.split())) for text in texts]

    total_weight = sum(weights)

    divisions = [divmod(total_tokens * weight, total_weight) for weight in weights]

    allocated = [q for q, _ in divisions]
    remaining = total_tokens - sum(allocated)

    order = sorted(
        range(len(texts)),
        key=lambda i: (-divisions[i][1], texts[i], i),
    )

    for i in order[:remaining]:
        allocated[i] += 1

    return allocated


def embed_openai_compatible(
    texts: list[str],
    route: ModelRoute,
    *,
    client: httpx.Client | None = None,
) -> EmbeddingResult:

    _validate_inputs(texts)

    if route.backend != "openai_compatible" or not route.base_url:
        raise ValueError(
            f"Invalid OpenAI-compatible route configuration for model '{route.upstream_model}'."
        )

    url = f"{route.base_url.rstrip('/')}/embeddings"

    body = {
        "model": route.upstream_model,
        "input": texts,
        "encoding_format": "float",
    }

    headers = _auth_headers(route)

    started_at = time.perf_counter()

    if client is None:
        with httpx.Client(timeout=120.0) as session:
            response = session.post(url, json=body, headers=headers)
    else:
        response = client.post(url, json=body, headers=headers, timeout=120.0)

    latency_s = time.perf_counter() - started_at

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise TypeError("Invalid response format: expected a JSON object.")

    entries = data.get("data")

    if not isinstance(entries, list):
        raise TypeError("Invalid response format: 'data' field must be a list.")

    ordered: list[Any] = [None] * len(texts)
    seen = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError(
                "Invalid response format: each entry must be a JSON object."
            )

        index = entry.get("index")

        if type(index) is not int or not 0 <= index < len(texts) or index in seen:
            raise ValueError(f"Invalid or duplicate index in response: {index}")

        seen.add(index)
        ordered[index] = entry.get("embedding")

    if len(seen) != len(texts):
        raise ValueError(
            f"Response is missing embeddings for some inputs. Expected {len(texts)} but got {len(seen)}."
        )

    vectors = _validate_vectors(ordered, len(texts))

    usage = data.get("usage", {})

    if not isinstance(usage, dict):
        raise TypeError("Invalid response format: 'usage' field must be a JSON object.")

    tokens_in = usage.get("prompt_tokens")

    if tokens_in is not None and (type(tokens_in) is not int or tokens_in < 0):
        raise ValueError(f"Invalid 'prompt_tokens' value in response: {tokens_in}")

    tokens_in_by_text = None

    if tokens_in is not None:
        tokenizer = (
            _load_nomic_tokenizer()
            if route.upstream_model.split(":", 1)[0] == "nomic-embed-text"
            else None
        )

        tokens_in_by_text = _apportion_embedding_tokens(texts, tokens_in, tokenizer)

    return EmbeddingResult(
        vectors=vectors,
        tokens_in=tokens_in,
        latency_s=latency_s,
        tokens_in_by_text=tokens_in_by_text,
    )


def embed_fastembed(
    texts: list[str],
    route: ModelRoute,
    *,
    model: Any,
) -> EmbeddingResult:

    _validate_inputs(texts)

    if route.backend != "fastembed":
        raise ValueError(
            f"Invalid FastEmbed route configuration for model '{route.upstream_model}'."
        )

    started_at = time.perf_counter()

    raw_vectors = list(model.embed(texts))

    vectors = _validate_vectors(raw_vectors, len(texts))

    tokens_counts = [model.token_count([text]) for text in texts]

    if any(
        isinstance(count, bool) or not isinstance(count, Integral) or count < 0
        for count in tokens_counts
    ):
        raise ValueError(
            f"Invalid token counts returned by FastEmbed model: {tokens_counts}"
        )

    tokens_in_by_text = [int(count) for count in tokens_counts]
    tokens_in = sum(tokens_in_by_text)

    latency_s = time.perf_counter() - started_at

    return EmbeddingResult(
        vectors=vectors,
        tokens_in=tokens_in,
        latency_s=latency_s,
        tokens_in_by_text=tokens_in_by_text,
    )


def embed_google_genai(
    texts: list[str],
    route: ModelRoute,
    google: GoogleConfig,
    *,
    output_dimensionality: int | None = None,
    client: Any | None = None,
) -> EmbeddingResult:
    """Synchronous Vertex AI Gemini embeddings.

    Current gemini-embedding-001 use is one input per upstream request; the
    gateway dispatches each missing input separately for rate-limit accounting.
    Token counts come from Vertex embedding statistics, never estimates.
    """
    _validate_inputs(texts)
    if route.backend != "google_genai" or route.task != "embeddings":
        raise ValueError("Invalid Google embedding model route")
    if not google.project_id:
        raise ValueError("Google project_id is not configured")
    if output_dimensionality is None:
        output_dimensionality = route.embedding_dimensions or 768
    if type(output_dimensionality) is not int or output_dimensionality <= 0:
        raise ValueError("Invalid embedding dimension")
    started_at = time.perf_counter()
    owned = client is None
    if owned:
        client = genai.Client(
            vertexai=True,
            project=google.project_id,
            location=route.location or google.location,
            http_options=types.HttpOptions(timeout=20000),
        )
    try:
        vectors = []
        counts: list[int] = []
        for text in texts:
            response = client.models.embed_content(
                model=route.upstream_model,
                contents=text,
                config=types.EmbedContentConfig(
                    output_dimensionality=output_dimensionality
                ),
            )
            embeddings = response.embeddings
            if not embeddings or len(embeddings) != 1:
                raise ValueError("Google returned an invalid embedding count")
            emb = embeddings[0]
            vectors.append(emb.values)
            stats = getattr(emb, "statistics", None)
            count = getattr(stats, "token_count", None)
            if count is None:
                counts.append(-1)
            elif (
                isinstance(count, bool)
                or not isinstance(count, (int, float))
                or not isfinite(count)
                or count < 0
                or int(count) != count
            ):
                raise ValueError("Invalid Google embedding token count")
            else:
                counts.append(int(count))
    finally:
        if owned:
            client.close()
    valid = _validate_vectors(vectors, len(texts))
    all_known = all(count >= 0 for count in counts)
    return EmbeddingResult(
        vectors=valid,
        tokens_in=sum(counts) if all_known else None,
        latency_s=time.perf_counter() - started_at,
        tokens_in_by_text=counts if all_known else None,
    )
