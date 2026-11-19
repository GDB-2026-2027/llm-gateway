# Configuration reference

This page explains the editable TOML files. Start with the [student manual](LLM_GATEWAY_STUDENT_MANUAL.pdf) for setup and first use. `bash setup.sh` copies `gateway.toml.example` and `models.toml.example` to local files only when those files do not already exist. Edit the local copies; do not put credentials in TOML or commit `*.local.env` files.

## What should I edit?

| Goal | Files or settings to change |
| --- | --- |
| Use the bundled Ollama routes | Usually none in TOML; install the named models in Ollama. Change `base_url` in `models.toml` only if Ollama listens elsewhere. |
| Use Google Vertex AI | Set `[google].project_id` in `gateway.toml`, configure Application Default Credentials, and check the model and region named in `models.toml`. |
| Use Azure | Replace the example Azure `base_url` and `upstream_model` values in `models.toml`; set the variable named by `api_key_env` in the environment or `azure.local.env`. |
| Share cache entries through B2 | Enable and fill `[cache_sync]` in `gateway.toml`; create `b2.local.env`. See [CACHE_B2.md](CACHE_B2.md). |
| Add or rename a route | Add a `[models."public/name"]` table in `models.toml`; use that public name in HTTP requests and the price list. |
| Estimate costs | Set `[pricing].prices_path` and update the price list at that path. Example cloud prices are estimates, not current invoices. |

## `gateway.toml`

The values below are those in [gateway.toml.example](../gateway.toml.example). Paths in this file are resolved relative to the directory containing `gateway.toml` unless absolute. Optional sections have code defaults, but keeping the example structure makes configuration easier to inspect.

| Section and key | Example | Meaning and when to change it |
| --- | --- | --- |
| `[server].host` | `"127.0.0.1"` | Listening address. Keep loopback for local clients; use an address reachable from a container or another machine only when needed. |
| `[server].port` | `8000` | Listening TCP port, integer from 1 to 65535. Change if the port is occupied, and update clients' URLs. |
| `[cache].database_path` | `"data/cache.sqlite3"` | Local SQLite cache file. Use a separate file for experiments that must not share cache entries. |
| `[routing].models_path` | `"models.toml"` | File that defines public model routes. |
| `[retries].invalid_output` | `1` | Additional generations after a response fails JSON/schema validation; integer 0–5. A value of 1 allows two generations in total. |
| `[retries].http_errors` | `2` | Additional attempts for retryable upstream HTTP 429 and 5xx errors; integer 0–5. This is separate from invalid-output retries. |
| `[retries].http_initial_delay_s` | `0.5` | Initial delay in seconds between retryable HTTP attempts; nonnegative number. |
| `[retries].http_max_delay_s` | `4.0` | Maximum retry delay in seconds; must be greater than `http_initial_delay_s`. |
| `[logging].log_path` | `"data/calls.jsonl"` | Local JSONL call log. This log is not sent to B2. |
| `[guardrails].max_body_bytes` | `65536` | Maximum HTTP request body size in bytes for `/v1/responses` and other write endpoints except embeddings. |
| `[guardrails].max_embeddings_body_bytes` | `4194304` | Maximum body size in bytes for `/v1/embeddings` (4 MiB in the example). |
| `[guardrails].max_google_embedding_texts` | `16` | Maximum number of input positions in one Google embedding HTTP request. Other embedding backends are not subject to this text-count limit. |
| `[guardrails].max_output_tokens` | `512` | Output-token limit sent to response providers. It is configured for the server, not per HTTP request. |
| `[guardrails].requests_per_minute` | `500` | Local spacing limit on upstream attempt starts, maintained separately for each provider key. |
| `[guardrails].max_concurrent_requests` | `5` | Maximum simultaneous upstream attempts for each provider key in this process. |
| `[guardrails].max_rate_wait_s` | `30.0` | Maximum time in seconds to wait for a local provider rate/concurrency slot; nonnegative number. |
| `[guardrails.run_limits].default_max_attempts_per_model` | `10000` | Maximum upstream attempts for each `(run_id, public model)` in this server process. Retries count; counters reset on restart. |
| `[guardrails.run_limits.models]` | See below | Optional per-public-model positive integer overrides of the default attempt limit. |
| `[pricing].prices_path` | `"prices/test_prices.toml"` | Price list used for estimated `cost_usd` in logs. When omitted, pricing is disabled. |
| `[google].project_id` | `""` | Google Cloud project ID for Vertex AI. An empty value is allowed at startup; set it before using Google routes. |
| `[google].location` | `"global"` | Default Vertex AI location. A Google model route can override it with `location` in `models.toml`. |

All guardrail counts and byte limits must be positive integers; `max_rate_wait_s` must be a finite nonnegative number. These are **local process limits**, not provider quotas or a shared spending budget. To override attempts for one public model, add this optional table:

```toml
[guardrails.run_limits.models]
"local/qwen2.5:3b" = 500
```

### `[cache_sync]` in `gateway.toml`

This section is read by `cache status`, `cache pull`, and `cache push`. The server does not sync automatically. Set `enabled = true` before using these commands.

| Key | Example | Meaning and validation |
| --- | --- | --- |
| `enabled` | `false` | Must be `true` for any `cache` command. |
| `provider` | `"backblaze_b2"` | Optional; this is the only accepted provider. |
| `bucket` | `"GROUP_BUCKET_NAME"` | Name of the group's actual B2 bucket; replace the placeholder. |
| `endpoint` | `"https://s3.eu-central-003.backblazeb2.com"` | S3-compatible HTTPS endpoint for that bucket's region. The parser accepts a Backblaze `s3.<region>.backblazeb2.com` host, without credentials, port, query, or extra path. |
| `compatibility_id` | `"semops-2026-v1"` | Shared remote namespace for one compatible group configuration. Use only letters, digits, dots, underscores, or hyphens, starting with a letter or digit. It does **not** verify that models or prompts match. |
| `credentials_file` | `"b2.local.env"` | Local credentials file path, relative to `gateway.toml` unless absolute. |
| `max_entries_per_archive` | `1000` | Positive maximum number of cache rows in one published archive. |
| `max_archive_mb` | `10` | Positive archive target size in MiB. A single row cannot be split. |

`SEMOPS_STUDENT_ID` belongs in `b2.local.env`, **not** in `gateway.toml`. Each installation needs its own ID; group members share the bucket, endpoint, and `compatibility_id`. See [CACHE_B2.md](CACHE_B2.md) for the synchronization workflow and conflicts.

## `models.toml`

Each `[models."public/name"]` table defines one public name that clients send as `model`. Public names are also used to look up prices and to scope run limits. The [models.toml.example](../models.toml.example) file includes local, Google, Azure, and smoke-test routes.

| Route key | Meaning and rules |
| --- | --- |
| `backend` | Required. Supported values are `openai_compatible`, `fastembed`, `google_genai`, and `microsoft_decision`. These names select existing adapters; an unsupported protocol needs code changes as described in the student manual. |
| `task` | `responses` or `embeddings`; defaults to `responses` when omitted. `fastembed` requires `embeddings`; `microsoft_decision` requires `responses`. |
| `upstream_model` | Required nonempty identifier sent to the selected provider. It may differ from the public name. Replace example identifiers with models/deployments available to your account. |
| `base_url` | Required for `openai_compatible` and `microsoft_decision`; a service base URL, not a gateway URL. The OpenAI-compatible adapter appends `/chat/completions` or `/embeddings`; the Microsoft Decision adapter expects a resource root. FastEmbed must not set this field. |
| `api_key_env` | Name of an environment variable containing a provider key, not the key itself. Required for `microsoft_decision`, optional for `openai_compatible`, and forbidden for `fastembed`. For OpenAI-compatible routes, a key with a nonlocal HTTP URL is rejected unless the URL uses HTTPS. |
| `json_schema_mode` | Required for `responses`: `native` asks the provider to enforce the JSON schema; `prompt` asks for JSON in the prompt and validates locally. Not applicable to embeddings. `microsoft_decision` requires `native`. Provider support for native mode must be checked with a test call. |
| `top_logprobs` | Optional nonnegative integer for response routes. A positive value requests the route's configured log-probability depth; a client cannot ask for more. Not applicable to embeddings, and `microsoft_decision` does not support a positive value. |
| `location` | Optional nonempty Vertex AI location for a `google_genai` route; overrides `[google].location`. The example embedding route uses `europe-west4`. |
| `embedding_dimensions` | Optional positive integer for an embedding route. Google uses it to request the vector dimension; cached and returned vectors are checked against it. |

The example Azure URLs are placeholders. `microsoft_decision` also requires `api_key_env` and `json_schema_mode = "native"`. `fastembed` requires no `base_url` or `api_key_env`; its model weights may be downloaded on first use. Google routes use Vertex AI project/location from configuration and Application Default Credentials, not a Gemini Developer API key. A route can be present without working credentials; a successful `/health` or `doctor` check does not prove that a provider model is available.

## Price list TOML

`[pricing].prices_path` points to a file with a required nonempty `[price_list].id` and one `[models."public/name"]` table per priced route. Each table has `input_per_million_usd` and `output_per_million_usd` as **quoted, nonnegative decimal strings** in USD per million tokens:

```toml
[price_list]
id = "group-prices-2026-11"

[models."local/qwen2.5:3b"]
input_per_million_usd = "0"
output_per_million_usd = "0"
```

Use the exact public name from `models.toml`. Update cloud rates for your actual provider, model, deployment, and region before using estimates; the [starter list](../prices/test_prices.toml) contains planning values only. The gateway can call a route without a price, but its `cost_usd` may be `null`; the separate run builder described in the student manual requires a price-list entry for models in a run, including zero-priced local models. Even with a price, unknown token counts can make a nonzero cost estimate unavailable. A cache hit may show the original usage priced at the current list; that is a theoretical value, not a new provider charge.

## Local credential files

`start server` loads `azure.local.env` when present, or a different file selected with `--env PATH`. It also loads `google.local.env` when present. Already exported variables take precedence. `--no-env` skips the selected `--env` file; the current CLI still reads `google.local.env` if it exists. The B2 commands read the path in `[cache_sync].credentials_file` separately. Example templates are in [`templates/`](../templates/). Keep real keys and credential files outside Git.
