import tomllib
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ModelRoute:
    backend: Literal["openai_compatible", "fastembed", "google_genai", "microsoft_decision"]
    upstream_model: str
    base_url: str | None = None
    api_key_env: str | None = None
    task: Literal["responses", "embeddings"] = "responses"
    top_logprobs: int | None = None
    json_schema_mode: Literal["native", "prompt"] | None = None
    location: str | None = None  # Overrides the default Google Vertex region.
    embedding_dimensions: int | None = None


@dataclass(frozen=True)
class GuardrailsConfig:
    max_body_bytes: int = 65_536
    max_embeddings_body_bytes: int = 4_194_304  # 4 MiB.
    max_google_embedding_texts: int = 16  # Bounds synchronous Google calls.
    max_output_tokens: int = 512

    requests_per_minute: int = 500
    max_concurrent_requests: int = 5
    max_rate_wait_s: float = 30.0

    default_max_attempts_per_model: int = 10_000
    max_attempts_by_model: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        integer_fields = (
            "max_body_bytes",
            "max_output_tokens",
            "requests_per_minute",
            "max_concurrent_requests",
            "default_max_attempts_per_model",
            "max_embeddings_body_bytes",
            "max_google_embedding_texts",
        )

        for field_name in integer_fields:
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")

        if (
            type(self.max_rate_wait_s) not in (int, float)
            or self.max_rate_wait_s < 0
            or not isfinite(self.max_rate_wait_s)
        ):
            raise ValueError("max_rate_wait_s must be a non-negative finite number.")

        for model, limit in self.max_attempts_by_model.items():
            if type(model) is not str or not model.strip():
                raise ValueError(
                    f"Invalid model name '{model}' in max_attempts_by_model."
                )
            if type(limit) is not int or limit <= 0:
                raise ValueError(
                    f"Limit for model '{model}' must be a positive integer."
                )


@dataclass(frozen=True)
class GoogleConfig:
    project_id: str | None = None
    location: str = "global"


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    database_path: Path
    models: dict[str, ModelRoute] = field(default_factory=dict)
    invalid_output_retries: int = 1  # One additional generation after invalid JSON.
    http_error_retries: int = 2
    http_initial_delay_s: float = 0.5
    http_max_delay_s: float = 4.0
    log_path: Path | None = None
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    prices_path: Path | None = None
    google: GoogleConfig = field(default_factory=GoogleConfig)


def load_model_routes(path: str | Path) -> dict[str, ModelRoute]:
    models_path = Path(path).expanduser().resolve()

    with models_path.open("rb") as f:
        data = tomllib.load(f)

    models_data = data.get("models")

    if not isinstance(models_data, dict) or not models_data:
        raise ValueError("Missing or invalid 'models' section in models file.")

    routes = {}

    for model_name, route_data in models_data.items():

        if not model_name.strip():
            raise ValueError("Model name must be a non-empty string.")

        if not isinstance(route_data, dict):
            raise TypeError(f"Invalid route data for model '{model_name}'.")

        backend = route_data.get("backend")
        task = route_data.get("task", "responses")
        upstream_model = route_data.get("upstream_model")
        base_url = route_data.get("base_url")
        api_key_env = route_data.get("api_key_env")
        top_logprobs = route_data.get("top_logprobs")
        json_schema_mode = route_data.get("json_schema_mode")
        location = route_data.get("location")
        embedding_dimensions = route_data.get("embedding_dimensions")

        if backend not in ("openai_compatible", "fastembed", "google_genai", "microsoft_decision"):
            raise ValueError(f"Invalid backend '{backend}' for model '{model_name}'.")

        if task not in ("responses", "embeddings"):
            raise ValueError(f"Invalid task '{task}' for model '{model_name}'.")

        if backend == "fastembed":
            if task != "embeddings":
                raise ValueError(
                    f"Invalid task '{task}' for model '{model_name}' with backend 'fastembed'."
                )

            if base_url is not None or api_key_env is not None:
                raise ValueError(
                    f"Unexpected base_url or api_key_env for model '{model_name}' with backend 'fastembed'."
                )

        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise ValueError(f"Invalid upstream_model for model '{model_name}'.")

        if api_key_env is not None and (
            not isinstance(api_key_env, str) or not api_key_env.strip()
        ):
            raise ValueError(f"Invalid api_key_env for model '{model_name}'.")

        if backend == "openai_compatible" and (
            not isinstance(base_url, str)
            or not base_url.startswith(("http://", "https://"))
        ):
            raise ValueError(f"Invalid base_url for model '{model_name}'.")

        if backend == "microsoft_decision":
            if task != "responses":
                raise ValueError(f"System One model {model_name!r} supports only responses")
            if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError(f"System One model {model_name!r} requires a resource base_url")
            if not isinstance(api_key_env, str) or not api_key_env.strip():
                raise ValueError(f"System One model {model_name!r} requires api_key_env")
            if json_schema_mode != "native":
                raise ValueError(f"System One model {model_name!r} requires json_schema_mode='native'")
            if top_logprobs not in (None, 0):
                raise ValueError(f"System One model {model_name!r} does not support token logprobs")
            if embedding_dimensions is not None or location is not None:
                raise ValueError(f"Unexpected embedding_dimensions or location for System One model {model_name!r}")

        if top_logprobs is not None and (
            type(top_logprobs) is not int or top_logprobs < 0
        ):
            raise ValueError(
                f"Invalid top_logprobs value for model '{model_name}'. Must be a non-negative integer."
            )

        if task == "responses" and json_schema_mode not in ("native", "prompt"):
            raise ValueError(
                f"Invalid json_schema_mode '{json_schema_mode}' for model '{model_name}'. Must be 'native' or 'prompt'."
            )

        if location is not None and (backend != "google_genai" or not isinstance(location, str) or not location.strip()):
            raise ValueError(f"Invalid Google location for model {model_name!r}")
        if embedding_dimensions is not None and (task != "embeddings" or type(embedding_dimensions) is not int or embedding_dimensions <= 0):
            raise ValueError(f"Invalid embedding_dimensions for model {model_name!r}")
        if task == "embeddings" and (
            json_schema_mode is not None or top_logprobs is not None
        ):
            raise ValueError(
                f"top_logprobs and json_schema_mode are not applicable for embeddings task in model '{model_name}'."
            )

        routes[model_name] = ModelRoute(
            backend=backend,
            upstream_model=upstream_model,
            base_url=base_url,
            api_key_env=api_key_env,
            task=task,
            top_logprobs=top_logprobs,
            json_schema_mode=json_schema_mode,
            location=location,
            embedding_dimensions=embedding_dimensions,
        )

    return routes


def load_config(path: str | Path = "gateway.toml") -> GatewayConfig:
    config_path = Path(path).expanduser().resolve()

    with config_path.open("rb") as f:
        config_data = tomllib.load(f)

    for section in ("server", "cache"):
        if section not in config_data:
            raise ValueError(f"Missing section '{section}' in configuration file.")

    server = config_data["server"]
    cache = config_data["cache"]

    host = server.get("host")
    port = server.get("port")
    database_path = cache.get("database_path")

    if not isinstance(host, str) or not host.strip():
        raise ValueError("server.host must be a non-empty string.")

    if type(port) is not int or not (0 < port < 65536):
        raise ValueError("server.port must be an integer between 1 and 65535.")

    if not isinstance(database_path, str) or not database_path.strip():
        raise ValueError("cache.database_path must be a non-empty string.")

    database_path = Path(database_path).expanduser()

    if not database_path.is_absolute():
        database_path = config_path.parent / database_path

    routing = config_data.get("routing")

    if not isinstance(routing, dict):
        raise TypeError("Missing or invalid 'routing' section in configuration file.")

    models_path = routing.get("models_path")

    if not isinstance(models_path, str) or not models_path.strip():
        raise ValueError("routing.models_path must be a non-empty string.")

    models_file = Path(models_path).expanduser()

    if not models_file.is_absolute():
        models_file = config_path.parent / models_file

    models = load_model_routes(models_file)

    # Keep Google routes available without requiring Google configuration at startup.
    # The adapter checks the project ID when one of those routes is used.
    raw_google = config_data.get("google", {})

    if not isinstance(raw_google, dict):
        raise TypeError("Invalid 'google' section in configuration file.")

    raw_project_id = raw_google.get("project_id", "")
    location = raw_google.get("location", "global")

    if not isinstance(raw_project_id, str):
        raise TypeError("google.project_id must be a string.")

    if not isinstance(location, str) or not location.strip():
        raise ValueError("google.location must be a non-empty string.")

    project_id = raw_project_id.strip() or None

    google = GoogleConfig(
        project_id=project_id,
        location=location.strip(),
    )

    retries = config_data.get("retries", {})

    if not isinstance(retries, dict):
        raise TypeError("Invalid 'retries' section in configuration file.")

    invalid_output_retries = retries.get("invalid_output", 1)

    if (
        type(invalid_output_retries)
    ) is not int or not 0 <= invalid_output_retries <= 5:
        raise ValueError(
            "retries.invalid_output must be a non-negative integer between 0 and 5."
        )

    http_error_retries = retries.get("http_errors", 2)
    http_initial_delay_s = retries.get("http_initial_delay_s", 0.5)
    http_max_delay_s = retries.get("http_max_delay_s", 4.0)

    if type(http_error_retries) is not int or not 0 <= http_error_retries <= 5:
        raise ValueError(
            "retries.http_errors must be a non-negative integer between 0 and 5."
        )

    if (
        type(http_initial_delay_s) not in (int, float)
        or not isfinite(http_initial_delay_s)
        or http_initial_delay_s < 0
    ):
        raise ValueError("retries.http_initial_delay_s must be a non-negative number.")

    if (
        type(http_max_delay_s) not in (int, float)
        or not isfinite(http_max_delay_s)
        or http_max_delay_s < 0
        or http_max_delay_s <= http_initial_delay_s
    ):
        raise ValueError(
            "retries.http_max_delay_s must be a non-negative number and greater than http_initial_delay_s."
        )

    logging_config = config_data.get("logging")

    if not isinstance(logging_config, dict):
        raise TypeError("Invalid 'logging' section in configuration file.")

    raw_log_path = logging_config.get("log_path")

    if not isinstance(raw_log_path, str) or not raw_log_path.strip():
        raise ValueError("logging.log_path must be a non-empty string.")

    log_path = Path(raw_log_path).expanduser()

    if not log_path.is_absolute():
        log_path = config_path.parent / log_path

    raw_guardrails = config_data.get("guardrails", {})

    if not isinstance(raw_guardrails, dict):
        raise TypeError("Invalid 'guardrails' section in configuration file.")

    raw_run_limits = raw_guardrails.get("run_limits", {})

    if not isinstance(raw_run_limits, dict):
        raise TypeError(
            "Invalid 'guardrails.attempt_limit' section in configuration file."
        )

    raw_model_limits = raw_run_limits.get("models", {})

    if not isinstance(raw_model_limits, dict):
        raise TypeError(
            "Invalid 'guardrails.attempt_limit.models' section in configuration file."
        )

    defaults = GuardrailsConfig()

    guardrails = GuardrailsConfig(
        max_body_bytes=raw_guardrails.get("max_body_bytes", defaults.max_body_bytes),
        max_embeddings_body_bytes=raw_guardrails.get(
            "max_embeddings_body_bytes", defaults.max_embeddings_body_bytes
        ),
        max_google_embedding_texts=raw_guardrails.get(
            "max_google_embedding_texts", defaults.max_google_embedding_texts
        ),
        max_output_tokens=raw_guardrails.get(
            "max_output_tokens", defaults.max_output_tokens
        ),
        requests_per_minute=raw_guardrails.get(
            "requests_per_minute", defaults.requests_per_minute
        ),
        max_concurrent_requests=raw_guardrails.get(
            "max_concurrent_requests",
            defaults.max_concurrent_requests,
        ),
        max_rate_wait_s=raw_guardrails.get("max_rate_wait_s", defaults.max_rate_wait_s),
        default_max_attempts_per_model=raw_run_limits.get(
            "default_max_attempts_per_model",
            defaults.default_max_attempts_per_model,
        ),
        max_attempts_by_model=dict(raw_model_limits),
    )

    pricing_config = config_data.get("pricing", {})

    if not isinstance(pricing_config, dict):
        raise TypeError("Invalid 'pricing' section in configuration file.")

    raw_prices_path = pricing_config.get("prices_path")

    prices_path = None

    if raw_prices_path is not None:
        if not isinstance(raw_prices_path, str) or not raw_prices_path.strip():
            raise ValueError("pricing.prices_path must be a non-empty string.")

        prices_path = Path(raw_prices_path).expanduser()

        if not prices_path.is_absolute():
            prices_path = config_path.parent / prices_path

        prices_path = prices_path.resolve()

    return GatewayConfig(
        host=host,
        port=port,
        database_path=database_path.resolve(),
        models=models,
        invalid_output_retries=invalid_output_retries,
        log_path=log_path.resolve(),
        http_error_retries=http_error_retries,
        http_initial_delay_s=http_initial_delay_s,
        http_max_delay_s=http_max_delay_s,
        guardrails=guardrails,
        prices_path=prices_path,
        google=google,
    )
