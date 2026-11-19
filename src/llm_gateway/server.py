"""FastAPI application setup for the LLM gateway."""

from contextlib import asynccontextmanager
from threading import Lock

import uvicorn
from fastapi import FastAPI

from .attempt_limit import RunAttemptLimiter
from .body_limit import RequestBodyLimitMiddleware
from .cache import GatewayCache
from .call_log import JsonlCallLog
from .config import GatewayConfig, load_config
from .embeddings import register_embeddings
from .pricing import load_price_list
from .rate_limit import ProviderThrottle, RateLimits
from .responses import register_responses
from .singleflight import KeyedLockPool


def create_app(config: GatewayConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Share cache and logging resources for the app's lifetime.
        app.state.price_list = (
            load_price_list(config.prices_path) if config.prices_path else None
        )
        app.state.cache = GatewayCache(config.database_path)
        app.state.fastembed_models = {}
        app.state.fastembed_models_lock = Lock()
        app.state.singleflight = KeyedLockPool()
        app.state.call_log = JsonlCallLog(config.log_path) if config.log_path else None
        try:
            yield
        finally:
            app.state.cache.close()

    app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
    guardrails = config.guardrails
    app.state.run_attempt_limiter = RunAttemptLimiter(
        default_max_attempts_per_model=guardrails.default_max_attempts_per_model,
        max_attempts_by_model=guardrails.max_attempts_by_model,
    )
    app.state.rate_limit = ProviderThrottle(
        RateLimits(
            requests_per_minute=guardrails.requests_per_minute,
            max_concurrent=guardrails.max_concurrent_requests,
            max_wait_s=guardrails.max_rate_wait_s,
        )
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=guardrails.max_body_bytes,
        max_embeddings_body_bytes=guardrails.max_embeddings_body_bytes,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    register_responses(app, config)
    register_embeddings(app, config)
    return app


def main() -> None:
    config = load_config()
    uvicorn.run(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
