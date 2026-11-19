"""Embeddings endpoint: per-text cache and log, synchronous provider calls."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from google.genai.errors import APIError as GoogleAPIError

from .api_models import EmbeddingsRequest
from .attempt_limit import RunAttemptLimitExceeded
from .cache import CacheCorruptionError, embedding_cache_key
from .call_log import CallRecord
from .config import GatewayConfig
from .pricing import calculate_cost_usd
from .providers import embed_fastembed, embed_google_genai, embed_openai_compatible
from .rate_limit import ProviderLimitExceeded


def load_fastembed_model(model_name: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=model_name)


def register_embeddings(app: FastAPI, config: GatewayConfig) -> None:
    @app.post("/v1/embeddings")
    def create_embeddings(payload: EmbeddingsRequest) -> dict:
        route = config.models.get(payload.model)
        if route is None:
            raise HTTPException(status_code=404, detail="Unknown model")
        if route.task != "embeddings":
            raise HTTPException(status_code=422, detail="Model does not support embeddings")
        if route.backend not in ("openai_compatible", "fastembed", "google_genai"):
            raise HTTPException(status_code=501, detail="Embedding backend not implemented")

        texts = [payload.input] if isinstance(payload.input, str) else payload.input
        if (route.backend == "google_genai" and
                len(texts) > config.guardrails.max_google_embedding_texts):
            raise HTTPException(
                status_code=422,
                detail=("Too many Google embedding inputs for a synchronous request; "
                        f"send at most {config.guardrails.max_google_embedding_texts} texts per batch"),
            )
        namespace = payload.metadata.cache_namespace
        run_id = payload.metadata.run_id
        started_at = time.time()
        price_list = app.state.price_list
        model_price = price_list.models.get(payload.model) if price_list is not None else None
        unique_texts = list(dict.fromkeys(texts))
        per_text: dict[str, tuple[list[float], int | None, float | None, bool, bool, int]] = {}
        other_retries_by_text: dict[str, int] = {}
        # Keep input order for output and logging; lock the model to avoid duplicate batch work.
        with app.state.singleflight.hold(namespace, f"embeddings:{payload.model}"):
            missing = []
            for text in unique_texts:
                try:
                    cached = app.state.cache.get_embedding(namespace, payload.model, text, route)
                except CacheCorruptionError as exc:
                    logging.getLogger("uvicorn.error").error(
                        "cache_corrupt: %s (model=%s, run_id=%s)",
                        exc, payload.model, run_id, exc_info=True,
                    )
                    if app.state.call_log is not None:
                        app.state.call_log.write(CallRecord(
                            run_id=run_id, endpoint="embeddings", cache_namespace=namespace,
                            model=payload.model, key=exc.key,
                            cache_hit=False, parse_ok=None, tokens_in=None, tokens_out=None,
                            latency_s=None,
                            cost_usd=None, price_list_id=None,
                            attempts=0, ts=started_at,
                            error_type="cache_corrupt", error_detail=str(exc),
                            usage_complete=False,
                        ))
                    raise HTTPException(
                        status_code=503,
                        detail="Cached entry corrupted; no upstream call made. Check gateway logs.",
                    ) from exc
                if cached is None:
                    missing.append(text)
                else:
                    per_text[text] = (cached.vector, cached.tokens_in, cached.latency_s,
                                      True, cached.usage_complete, cached.attempts)

            total_fresh_tokens = 0
            fresh_usage_known = True
            attempts_total = 0
            other_retries_total = 0
            last_upstream_latency: float | None = None

            def log_error(error_type: str) -> None:
                if app.state.call_log is not None:
                    # Log one failure for the batch; no successful text records follow.
                    app.state.call_log.write(CallRecord(
                        run_id=run_id, endpoint="embeddings", cache_namespace=namespace,
                        model=payload.model,
                        key=embedding_cache_key(payload.model, missing[0] if missing else texts[0], route),
                        cache_hit=False, parse_ok=None, tokens_in=None, tokens_out=None,
                        latency_s=last_upstream_latency,
                        cost_usd=None, price_list_id=price_list.price_list_id if model_price else None,
                        attempts=attempts_total, ts=started_at, error_type=error_type,
                        other_retries=other_retries_total,
                        usage_complete=False,
                    ))

            # Each upstream call passes through rate limiting and claims one run attempt.
            def upstream(batch: list[str]):
                nonlocal attempts_total, other_retries_total, last_upstream_latency
                last_upstream_latency = None
                if route.backend == "fastembed":
                    provider_key = "fastembed"
                elif route.backend == "google_genai":
                    provider_key = f"google_genai:{config.google.project_id}"
                else:
                    parsed = urlsplit(route.base_url or "")
                    provider_key = f"{parsed.scheme}://{parsed.netloc}"
                for retry in range(config.http_error_retries + 1):
                    try:
                        with app.state.rate_limit.hold(provider_key):
                            if route.backend == "fastembed":
                                with app.state.fastembed_models_lock:
                                    model = app.state.fastembed_models.get(payload.model)
                                    if model is None:
                                        model = load_fastembed_model(route.upstream_model)
                                        app.state.fastembed_models[payload.model] = model
                            app.state.run_attempt_limiter.claim(run_id=run_id, model=payload.model)
                            attempts_total += 1
                            if retry > 0:
                                other_retries_total += 1
                            call_started = time.perf_counter()
                            try:
                                if route.backend == "fastembed":
                                    return embed_fastembed(batch, route, model=model)
                                if route.backend == "google_genai":
                                    return embed_google_genai(batch, route, config.google)
                                return embed_openai_compatible(batch, route)
                            finally:
                                last_upstream_latency = time.perf_counter() - call_started
                    except RunAttemptLimitExceeded as exc:
                        log_error("gateway_run_model_limit")
                        raise HTTPException(status_code=429, detail=str(exc)) from exc
                    except ProviderLimitExceeded as exc:
                        log_error("gateway_rate_limit")
                        raise HTTPException(status_code=429, detail=str(exc)) from exc
                    except (httpx.HTTPStatusError, GoogleAPIError) as exc:
                        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.code
                        if (status == 429 or 500 <= status < 600) and retry < config.http_error_retries:
                            time.sleep(min(config.http_initial_delay_s * (2 ** retry), config.http_max_delay_s))
                            continue
                        log_error(f"HTTP_{status}")
                        raise HTTPException(status_code=502, detail="Embedding backend request failed") from exc
                    except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                        log_error(type(exc).__name__)
                        raise HTTPException(status_code=502, detail="Embedding backend request failed") from exc
                raise RuntimeError("Unreachable")

            # Gemini uses one online call per missing text; other backends accept a batch.
            batches = [[x] for x in missing] if route.backend == "google_genai" else ([missing] if missing else [])
            for batch in batches:
                before = attempts_total
                before_other = other_retries_total
                result = upstream(batch)
                assert last_upstream_latency is not None
                if len(result.vectors) != len(batch):
                    log_error("invalid_vector_count")
                    raise HTTPException(status_code=502, detail="Invalid number of embeddings")
                counts = result.tokens_in_by_text
                if result.tokens_in is not None and (
                    counts is None or len(counts) != len(batch)
                    or any(type(n) is not int or n < 0 for n in counts)
                    or sum(counts) != result.tokens_in
                ):
                    log_error("invalid_token_counts")
                    raise HTTPException(status_code=502, detail="Invalid embedding token counts")
                if result.tokens_in is None:
                    fresh_usage_known = False
                else:
                    total_fresh_tokens += result.tokens_in
                for i, text in enumerate(batch):
                    count = counts[i] if counts is not None else None
                    # Attribute the final upstream call's duration in the same
                    # proportion as its token-based per-text cost. With unknown
                    # or zero total usage, no token weights exist: split evenly.
                    latency = (
                        last_upstream_latency * count / result.tokens_in
                        if count is not None and result.tokens_in is not None
                        and result.tokens_in > 0
                        else last_upstream_latency / len(batch)
                    )
                    attempts = attempts_total - before if i == 0 else 0
                    complete = count is not None and attempts_total - before == 1
                    vector = result.vectors[i]
                    app.state.cache.put_embedding(
                        namespace, payload.model, text, vector, tokens_in=count,
                        latency_s=latency, usage_complete=complete,
                        attempts=attempts, route=route,
                    )
                    per_text[text] = (vector, count, latency, False, complete, attempts)
                    other_retries_by_text[text] = other_retries_total - before_other if i == 0 else 0

            logger = app.state.call_log
            if logger is not None:
                seen = set()
                for text in texts:
                    vector, count, latency, cached, complete, attempts = per_text[text]
                    # Repeated input text reuses the first result but keeps its own log line.
                    hit = cached or text in seen
                    seen.add(text)
                    estimated_cost = (calculate_cost_usd(count, 0, model_price)
                                      if model_price is not None else None)
                    logger.write(CallRecord(
                        run_id=run_id, endpoint="embeddings", cache_namespace=namespace,
                        model=payload.model, key=embedding_cache_key(payload.model, text, route),
                        cache_hit=hit, parse_ok=True,
                        tokens_in=count, tokens_out=0, tokens_in_final=count,
                        tokens_out_final=0, tokens_in_last=count, tokens_out_last=0,
                        latency_s=latency, cost_usd=float(estimated_cost) if estimated_cost is not None else None,
                        price_list_id=price_list.price_list_id if model_price is not None else None,
                        attempts=attempts, ts=started_at, usage_complete=complete,
                        # Embeddings have no parse retries. A cached or
                        # duplicated input copies the original attempt count.
                        other_retries=(max(0, attempts - 1) if hit
                                       else other_retries_by_text.get(text, 0)),
                    ))

            result = {
                "object": "list", "model": payload.model,
                "data": [
                    {"object": "embedding", "index": i, "embedding": per_text[text][0]}
                    for i, text in enumerate(texts)
                ],
            }
            # Response usage counts only calls made for this request, not cache hits.
            if fresh_usage_known:
                result["usage"] = {"prompt_tokens": total_fresh_tokens, "total_tokens": total_fresh_tokens}
            return result
