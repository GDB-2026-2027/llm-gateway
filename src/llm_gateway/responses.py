import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from google.genai.errors import APIError as GoogleAPIError
from jsonschema.exceptions import SchemaError

from .api_models import ResponsesRequest
from .attempt_limit import RunAttemptLimitExceeded
from .cache import CacheCorruptionError, ResponseCacheEntry, response_cache_key
from .call_log import CallRecord
from .config import GatewayConfig, ModelRoute
from .microsoft_decision import (
    DecisionRequestError,
    call_microsoft_decision,
    validate_decision_request,
)
from .pricing import calculate_cost_usd
from .providers import call_google_genai, call_openai_compatible
from .rate_limit import ProviderLimitExceeded
from .validation import (
    InvalidLLMOutput,
    build_fallback,
    parse_and_validate_output,
    validate_schema,
)


def make_response(
    text: str,
    parse_ok: bool,
    *,
    provider_response: dict[str, Any] | None,
    error_type: str | None = None,
) -> dict[str, Any]:
    response = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        "parse_ok": parse_ok,
        "provider_response": provider_response,
    }
    if error_type is not None:
        response["error_type"] = error_type
    return response


def request_cache_key(
    payload: ResponsesRequest, route: ModelRoute, max_output_tokens: int
) -> str:
    return response_cache_key(
        model=payload.model,
        instructions=payload.instructions,
        input_messages=[message.model_dump() for message in payload.input],
        schema=payload.text.format.schema_definition,
        parameters={
            "format_name": payload.text.format.name,
            "temperature": payload.temperature,
            "top_logprobs": route.top_logprobs,
            "json_schema_mode": route.json_schema_mode,
            "max_output_tokens": max_output_tokens,
            "backend": route.backend,
            "upstream_model": route.upstream_model,
            "location": route.location,
        },
    )


def register_responses(app: FastAPI, config: GatewayConfig) -> None:
    guardrails = config.guardrails

    @app.post("/v1/responses")
    def create_response(payload: ResponsesRequest) -> dict:

        # Resolve the model and check the options it supports.
        route = config.models.get(payload.model)

        if route is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown model",
            )

        if route.task != "responses":
            raise HTTPException(
                status_code=422,
                detail="Model does not support responses",
            )

        if (
            payload.top_logprobs is not None
            and payload.top_logprobs > 0
            and (
                route.top_logprobs is None or payload.top_logprobs > route.top_logprobs
            )
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Requested top_logprobs exceeds "
                    "the model's configured capability."
                ),
            )

        # Validate the schema before using it for caching or generation.
        schema = payload.text.format.schema_definition

        try:
            validate_schema(schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=422,
                detail="Invalid JSON Schema",
            ) from exc

        # Reject unsupported backends and provider-specific requests early.
        if route.backend not in (
            "openai_compatible",
            "google_genai",
            "microsoft_decision",
        ):
            raise HTTPException(
                status_code=501,
                detail="Backend not implemented",
            )

        if route.backend == "openai_compatible":
            parsed_url = urlsplit(route.base_url or "")

            is_local = parsed_url.hostname in ("localhost", "127.0.0.1", "::1")

            if (
                route.api_key_env is not None
                and not is_local
                and parsed_url.scheme != "https"
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Insecure backend URL for API key",
                )

        # System One supports only Boolean and classification schemas.
        if route.backend == "microsoft_decision":
            try:
                validate_decision_request(payload, route)
            except DecisionRequestError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Include the request and route settings in the cache key.
        namespace = payload.metadata.cache_namespace

        key = request_cache_key(payload, route, guardrails.max_output_tokens)

        started_at = time.time()
        other_retries = 0

        def write_record(
            *,
            cache_hit: bool,
            parse_ok: bool | None,
            tokens_in: int | None,
            tokens_out: int | None,
            latency_s: float | None,
            attempts: int,
            parse_retries: int,
            error_type: str | None = None,
            error_detail: str | None = None,
            tokens_in_final: int | None = None,
            tokens_out_final: int | None = None,
            usage_complete: bool = False,
            tokens_in_last: int | None = None,
            tokens_out_last: int | None = None,
        ) -> None:
            """Log one complete gateway invocation."""

            logger = app.state.call_log

            if logger is None:
                return

            price_list = app.state.price_list

            cost_usd = None
            price_list_id = None

            if price_list is not None:
                model_price = price_list.models.get(payload.model)

                if model_price is not None:
                    price_list_id = price_list.price_list_id

                    observed_cost = calculate_cost_usd(
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        price=model_price,
                    )

                    if observed_cost is not None:
                        cost_usd = float(observed_cost)

            logger.write(
                CallRecord(
                    run_id=payload.metadata.run_id,
                    endpoint="responses",
                    cache_namespace=namespace,
                    model=payload.model,
                    key=key,
                    cache_hit=cache_hit,
                    parse_ok=parse_ok,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    tokens_in_final=tokens_in_final,
                    tokens_out_final=tokens_out_final,
                    usage_complete=usage_complete,
                    tokens_in_last=tokens_in_last,
                    tokens_out_last=tokens_out_last,
                    latency_s=latency_s,
                    cost_usd=cost_usd,
                    price_list_id=price_list_id,
                    attempts=attempts,
                    parse_retries=parse_retries,
                    other_retries=other_retries,
                    ts=started_at,
                    error_type=error_type,
                    error_detail=error_detail,
                )
            )

        with app.state.singleflight.hold(namespace, key):
            # Validate cached data; corruption must not trigger a provider call.
            try:
                cached = app.state.cache.get_response(namespace, key)
                if cached is not None:
                    try:
                        parse_and_validate_output(cached.text, schema)
                    except (InvalidLLMOutput, TypeError) as exc:
                        raise CacheCorruptionError(
                            "response_cache",
                            "text",
                            namespace,
                            key,
                            "cached answer is not valid for the requested schema",
                        ) from exc
            except CacheCorruptionError as exc:
                logging.getLogger("uvicorn.error").error(
                    "cache_corrupt: %s (model=%s, run_id=%s)",
                    exc,
                    payload.model,
                    payload.metadata.run_id,
                    exc_info=True,
                )
                write_record(
                    cache_hit=False,
                    parse_ok=None,
                    tokens_in=None,
                    tokens_out=None,
                    latency_s=None,
                    attempts=0,
                    parse_retries=0,
                    error_type="cache_corrupt",
                    error_detail=str(exc),
                )
                raise HTTPException(
                    status_code=503,
                    detail="Cached entry corrupted; no upstream call made. Check gateway logs.",
                ) from exc

            if cached is not None:
                cached_text = cached.text

                if isinstance(cached_text, str):
                    # Successful calls use one initial attempt; the remaining
                    # attempts are either parse or HTTP retries. Both counts
                    # can be recovered without changing the SQLite/B2 schema.
                    other_retries = max(0, cached.attempts - 1 - cached.parse_retries)
                    write_record(
                        cache_hit=True,
                        parse_ok=True,
                        tokens_in=cached.tokens_in,
                        tokens_out=cached.tokens_out,
                        tokens_in_final=cached.tokens_in_final,
                        tokens_out_final=cached.tokens_out_final,
                        usage_complete=cached.usage_complete,
                        tokens_in_last=cached.tokens_in_final,
                        tokens_out_last=cached.tokens_out_final,
                        latency_s=cached.latency_s,
                        attempts=cached.attempts,
                        parse_retries=cached.parse_retries,
                    )

                    return make_response(
                        cached_text,
                        True,
                        provider_response=cached.provider_response,
                    )

            # On a cache miss, start counting provider attempts and known usage.
            attempts = 0
            parse_retries = 0

            # Sum usage from responses received, including invalid JSON.
            tokens_in: int | None = 0
            tokens_out: int | None = 0

            # Keep the final valid generation separate from the total.
            tokens_in_final: int | None = None
            tokens_out_final: int | None = None
            tokens_in_last: int | None = None
            tokens_out_last: int | None = None

            # A failed HTTP attempt or missing usage makes the total incomplete.
            usage_complete = True

            # Diagnostic duration of the last provider invocation, never a retry total.
            latency_s: float | None = None

            # Parsing retries request another generation; HTTP retries repeat a failed call.
            parsed_url = urlsplit(route.base_url or "")

            if route.backend == "google_genai":
                provider_key = f"google_genai:{config.google.project_id}"
            else:
                provider_key = (
                    f"{parsed_url.scheme}://{parsed_url.netloc}"
                    if parsed_url.scheme and parsed_url.netloc
                    else route.backend
                )

            for output_attempt in range(config.invalid_output_retries + 1):

                # Retry only temporary HTTP errors within this generation.
                for http_retry in range(config.http_error_retries + 1):
                    try:
                        with app.state.rate_limit.hold(provider_key):

                            app.state.run_attempt_limiter.claim(
                                run_id=payload.metadata.run_id,
                                model=payload.model,
                            )
                            if output_attempt > 0 and http_retry == 0:
                                parse_retries += 1
                            attempts += 1
                            if http_retry > 0:
                                other_retries += 1
                            request_started = time.perf_counter()
                            try:
                                if route.backend == "google_genai":
                                    result = call_google_genai(
                                        payload,
                                        route,
                                        config.google,
                                        max_output_tokens=guardrails.max_output_tokens,
                                    )
                                elif route.backend == "microsoft_decision":
                                    result = call_microsoft_decision(payload, route)
                                else:
                                    result = call_openai_compatible(
                                        payload,
                                        route,
                                        max_output_tokens=guardrails.max_output_tokens,
                                    )
                            finally:
                                latency_s = time.perf_counter() - request_started

                    except RunAttemptLimitExceeded as exc:
                        write_record(
                            cache_hit=False,
                            parse_ok=None,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            tokens_in_final=None,
                            tokens_out_final=None,
                            usage_complete=usage_complete,
                            latency_s=latency_s,
                            attempts=attempts,
                            parse_retries=parse_retries,
                            error_type="gateway_run_model_limit",
                        )

                        raise HTTPException(
                            status_code=429,
                            detail=str(exc),
                        ) from exc

                    except ProviderLimitExceeded as exc:
                        write_record(
                            cache_hit=False,
                            parse_ok=None,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            tokens_in_final=None,
                            tokens_out_final=None,
                            usage_complete=usage_complete,
                            latency_s=latency_s,
                            attempts=attempts,
                            parse_retries=parse_retries,
                            error_type="gateway_rate_limit",
                        )

                        raise HTTPException(
                            status_code=429,
                            detail=str(exc),
                        ) from exc

                    except (httpx.HTTPStatusError, GoogleAPIError) as exc:
                        # The failed HTTP attempt may have consumed unknown tokens.
                        usage_complete = False

                        status = (
                            exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else exc.code
                        )

                        retryable = status == 429 or 500 <= status < 600

                        if retryable and http_retry < config.http_error_retries:
                            delay = min(
                                config.http_initial_delay_s * (2**http_retry),
                                config.http_max_delay_s,
                            )

                            time.sleep(delay)
                            continue

                        write_record(
                            cache_hit=False,
                            parse_ok=None,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            tokens_in_final=None,
                            tokens_out_final=None,
                            usage_complete=usage_complete,
                            latency_s=latency_s,
                            attempts=attempts,
                            parse_retries=parse_retries,
                            error_type=f"HTTP_{status}",
                        )

                        raise HTTPException(
                            status_code=502,
                            detail="LLM backend request failed",
                        ) from exc

                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        usage_complete = False

                        write_record(
                            cache_hit=False,
                            parse_ok=None,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            tokens_in_final=None,
                            tokens_out_final=None,
                            usage_complete=usage_complete,
                            latency_s=latency_s,
                            attempts=attempts,
                            parse_retries=parse_retries,
                            error_type=type(exc).__name__,
                        )

                        raise HTTPException(
                            status_code=502,
                            detail="LLM backend request failed",
                        ) from exc

                    # Account for this response before checking its JSON.
                    tokens_in_last = result.tokens_in
                    tokens_out_last = result.tokens_out

                    if result.tokens_in is None:
                        usage_complete = False
                    elif tokens_in is not None:
                        tokens_in += result.tokens_in

                    if result.tokens_out is None:
                        usage_complete = False
                    elif tokens_out is not None:
                        tokens_out += result.tokens_out

                    break

                # Validate locally; invalid JSON may trigger another generation.
                try:
                    parse_and_validate_output(
                        result.text,
                        schema,
                    )

                except InvalidLLMOutput:

                    if output_attempt < config.invalid_output_retries:
                        continue

                    # Parsing retries are exhausted; try a deterministic fallback.
                    try:
                        fallback = build_fallback(schema)

                    except ValueError as exc:

                        # Parsing failed, but usage may still be fully known.
                        write_record(
                            cache_hit=False,
                            parse_ok=False,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            tokens_in_final=None,
                            tokens_out_final=None,
                            usage_complete=usage_complete,
                            latency_s=latency_s,
                            attempts=attempts,
                            parse_retries=parse_retries,
                            error_type="unsupported_fallback",
                        )

                        raise HTTPException(
                            status_code=502,
                            detail="No fallback available",
                        ) from exc

                    response_text = json.dumps(
                        fallback,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )

                    parse_ok = False

                    # A fallback is not a valid provider generation.
                    tokens_in_final = None
                    tokens_out_final = None

                else:
                    response_text = result.text
                    parse_ok = True

                    # Store only the last valid generation in the final fields.
                    tokens_in_final = result.tokens_in
                    tokens_out_final = result.tokens_out

                # Cache valid provider output, never a fallback.
                if parse_ok:
                    assert latency_s is not None
                    entry = ResponseCacheEntry(
                        namespace=namespace,
                        key=key,
                        text=response_text,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        tokens_in_final=tokens_in_final,
                        tokens_out_final=tokens_out_final,
                        usage_complete=usage_complete,
                        latency_s=latency_s,
                        attempts=attempts,
                        parse_retries=parse_retries,
                        provider_response=result.provider_response,
                    )

                    app.state.cache.put_response(entry)

                # Log one record for the completed gateway request.
                write_record(
                    cache_hit=False,
                    parse_ok=parse_ok,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    tokens_in_final=tokens_in_final,
                    tokens_out_final=tokens_out_final,
                    usage_complete=usage_complete,
                    tokens_in_last=tokens_in_last,
                    tokens_out_last=tokens_out_last,
                    latency_s=latency_s,
                    attempts=attempts,
                    parse_retries=parse_retries,
                    error_type="parse_error" if not parse_ok else None,
                )

                return make_response(
                    response_text,
                    parse_ok,
                    provider_response=result.provider_response,
                    error_type="parse_error" if not parse_ok else None,
                )
