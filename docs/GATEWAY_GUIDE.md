# LLM Gateway operating guide

This guide starts from a newly cloned repository. Each student runs **a gateway on their own computer**. Clients, including graph database bindings, call its HTTP APIs. The gateway selects a provider based on the model name, uses a local SQLite cache, and records calls in a JSONL log. The group's shared cache on Backblaze B2 is exchanged only through manual commands.

The repository already provides the HTTP server, provider adapters, routing, JSON validation, retries, cache, logging, and B2 synchronization. Students develop the semantic operator library, bindings or graph database integration, and code that runs queries and collects results. The RDF/Neo4j engine evaluates the query and its bindings; the operator code decides **when** to call the gateway and constructs the prompt, schema, and metadata; the gateway calls the model. Students' instances do not communicate directly with each other: they exchange cache entries through the B2 commands in section 6.

## 1. Install the gateway

You need a Bash shell and `uv` available in the terminal (`uv --version`). If `uv` is missing, install it before proceeding; `setup.sh` reports its absence and points to installation instructions. The project requires **Python 3.12 or later**, and setup selects 3.12 by default (`SEMOPS_PYTHON_VERSION` lets you change it). `uv` can download Python if necessary. The scripts have been verified on macOS; their structure supports Linux with Bash, but that has not been tested here. No native Windows setup is documented: use a compatible Unix shell and verify it in your environment.

From the **root of the cloned repository**: `setup.sh` creates `gateway.toml` and `models.toml` from the `.example` copies only if the local files do not already exist.

```sh
bash setup.sh
source ./activate.sh
doctor
```

`bash setup.sh` installs the gateway and its dependencies in `.venv` using `pyproject.toml` and `uv.lock`. `source ./activate.sh` adds `start`, `cache`, `doctor`, and `runs` to the **current terminal's** `PATH` only; repeat it in each new terminal. `doctor` checks files, TOML, routes, and the price list, but does not test Google authentication, providers, or B2. Warnings about missing Azure or B2 configuration are normal if you do not use them. Alternatively, run `bin/start`, `bin/cache`, `bin/doctor`, and `bin/runs` directly from the repository root.

The default installation includes FastEmbed, support for configured providers, B2, and pytest. **It does not install Neo4j, GraphDB/RDF4J, RDFLib, or PyOxiGraph.** Graph databases are external to the gateway. For developers using Python RDF bindings, the optional groups are independent:

```sh
uv sync --locked --group rdflib
uv sync --locked --group oxigraph
uv sync --locked --group rdflib --group oxigraph  # both
```

`uv sync` synchronizes `.venv` exactly: a later `bash setup.sh` without the same groups can remove optional packages. With `uv run`, also specify the groups required by the script, for example `uv run --locked --group rdflib python script.py`.

## 2. Start and check the server

In the first terminal, from the repository root:

```sh
source ./activate.sh
start server
```

Keep this terminal open. The server listens at the `[server]` address in `gateway.toml`, initially `http://127.0.0.1:8000`. Stop it with `Ctrl+C`. In a second terminal:

```sh
source ./activate.sh
start status
curl -sS http://127.0.0.1:8000/health
```

`/health` returns `{"status":"ok"}` if the gateway is running; it **does not prove** that Ollama, Google, Azure, or B2 are reachable. The `http://127.0.0.1:8000/docs` page also shows the HTTP schemas. If you change the port in `gateway.toml`, change the URLs in this guide accordingly. `start status` reads the port from the configuration.

The server starts without Google, Azure, or B2 credentials. Provider credentials are needed only when you use that provider's route; B2 credentials are needed for synchronization commands. You do not need to install a graph database to start or call the gateway.

If Neo4j runs in Docker and the gateway runs on the host, `127.0.0.1` inside the container means **the container**, not the host. Set `genai.openai.baseurl` to a host address reachable from the container (often `host.docker.internal` on Docker Desktop), and make `[server].host` listen on a reachable interface. Restrict access to the intended network: the initial `127.0.0.1` setting accepts only local connections to the host.

## 3. Prepare a model and make the first call

`models.toml` defines the models' **public names**, backends, upstream models, and route options. HTTP requests use the public name. The initial `local/qwen2.5:3b` route points to Ollama at `http://127.0.0.1:11434/v1`. To try it, install and start Ollama separately, then download the model:

```sh
ollama pull qwen2.5:3b
```

Ollama must be running when you send the request (some installations start it as a service; otherwise use `ollama serve`). Here is a complete example for `POST /v1/responses`:

```sh
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/qwen2.5:3b",
  "instructions": "Respond only with JSON that conforms to the schema.",
  "input": [{"role": "user", "content": "Does 2 + 2 equal 4?"}],
  "text": {"format": {
    "type": "json_schema",
    "name": "boolean_answer",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {"answer": {"type": "boolean"}},
      "required": ["answer"],
      "additionalProperties": false
    }
  }},
  "metadata": {"run_id": "local-test-1", "cache_namespace": "demo-group"}
}
JSON
```

The response contains `output[0].content[0].text`: **a string containing JSON**, for example `{"answer":true}`. `parse_ok: true` means the model produced valid output. Repeating the same request in the same `cache_namespace` may serve it from the cache without calling the provider; `cache_hit` appears in the log, not in the HTTP response.

`metadata.run_id` identifies a run in logs and attempt limits; assign a recognizable identifier to each execution. `metadata.cache_namespace` separates caches: use the same value for requests that should share responses. `run_id` is not part of the cache key. **Both fields are required** in gateway requests.

### Embeddings

The `local/nomic-embed-text` route uses Ollama. After `ollama pull nomic-embed-text`:

```sh
curl -sS http://127.0.0.1:8000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"local/nomic-embed-text","input":["first text","second text"],"metadata":{"run_id":"embedding-test-1","cache_namespace":"demo-group"}}'
```

The response contains `data`, a list of vectors in input order, and `usage` when token counts for new calls are known. The gateway deduplicates identical texts within the request and caches each embedding separately. The `local/bge-small-en-v1.5` route instead uses FastEmbed: setup installs the package, but model weights may need to be downloaded on first use.

### How many calls does a query make?

The gateway does not know about SPARQL/Cypher bindings and **does not implement a batch of independent evaluations** for `/v1/responses`. The `input` field is a list of messages in **one conversation**: one HTTP request produces one structured result, one cache key and, on a miss, one generation (plus any retries). If a query evaluates an operator on 1,000 bindings and student code sends a request for each binding, the gateway receives 1,000 HTTP requests; cache hits avoid upstream calls but not HTTP requests. The actual number depends on the binding or plugin implementation, not just query cardinality.

To group multiple bindings, the operator library must construct and interpret the prompt and schema for that group, or reduce evaluations using a semantic block join in its algorithm. The gateway still treats the group as **one** response and does not separate cache entries, errors, or retries by binding. For example, a prompt requesting decisions for 10 items may use a list in the schema, but student code must associate each item with its decision and handle failure of the whole response.

`/v1/embeddings`, by contrast, accepts a list of texts in **one HTTP request** and returns a vector for each position. It deduplicates identical texts and checks the cache **per text**. On a miss, Ollama and FastEmbed receive an upstream batch of missing texts; Google receives one upstream call per missing text (at most 16 texts per HTTP request in the initial configuration). HTTP embedding batches, provider upstream batches, and semantic block joins in the database are different operations. Retries and limits apply to actual upstream calls.

## 4. What happens for each request

1. The gateway looks up the public name in `models.toml` and validates the request body.
2. It computes a canonical key and looks for a response in the SQLite cache namespace (`data/cache.sqlite3` in the initial configuration).
3. On a miss, it applies limits and retries, calls the provider, and validates the requested output. For responses, a generation with invalid JSON can be repeated up to `retries.invalid_output` times (initially 1, for at most 2 generations if no other errors occur). HTTP 429 and 5xx errors allow up to `retries.http_errors` additional attempts. Valid responses are cached; **a parsing fallback is not cached as a valid response**. Embeddings have no parsing fallback.
4. It returns the HTTP result and writes details to `data/calls.jsonl` (one JSON row per event according to each endpoint's rules).

For `/v1/responses`, `parse_ok: false` with `error_type: "parse_error"` means every attempt produced invalid output and the gateway returned an HTTP 200 fallback, if the schema is supported. By contrast, `{"answer":false}` with `parse_ok: true` is a valid negative decision. This metadata is **outside** the JSON string in `output[0].content[0].text`. For classification, the fallback uses a reserved label derived from `__PARSE_FAILURE__`, outside the enum of classes. If the schema does not support an implemented fallback, the gateway returns HTTP 502 and records `unsupported_fallback`.

A direct HTTP client can check `parse_ok`. In the verified version, the Neo4j GenAI plugin exposes only the structured content map to Cypher: to distinguish a Boolean fallback from a valid `false`, find the call's log row using `run_id` and its key/timestamp. Transport errors, timeouts, and limits return an HTTP error, have `parse_ok: null` in the log if the request reaches the call phase, and should not be counted as valid negative decisions or parsing fallbacks.

The JSONL log includes `run_id`, `cache_namespace`, `model`, key, `cache_hit`, `parse_ok`, attempts, retries, tokens, estimated cost, `latency_s`, and any `error_type`, among other fields. A `/v1/responses` request that reaches the cache/provider produces one row; a successful embedding request produces one row **per input position**, even if texts are duplicated. Requests rejected before the cache/provider phase may produce no row. On cache hits, retry counts and diagnostic duration come from the original call: `cache_hit: true` means they are **not** new retries. Response `tokens_in` and `tokens_out` aggregate known usage across attempts; `tokens_in_final`/`tokens_out_final` identify the final valid generation, while `tokens_in_last`/`tokens_out_last` identify the last generation even if invalid. `usage_complete` indicates whether usage is known for all attempts.

Illustrative example of **one JSONL row** for a successful local cache miss (key and token counts are examples):

```json
{"run_id":"local-test-1","endpoint":"responses","cache_namespace":"demo-group","model":"local/qwen2.5:3b","key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","cache_hit":false,"parse_ok":true,"tokens_in":12,"tokens_out":5,"latency_s":0.42,"cost_usd":0.0,"price_list_id":"local-dev-2026-11","attempts":1,"parse_retries":0,"other_retries":0,"ts":1794916800.0,"error_type":null,"error_detail":null,"tokens_in_final":12,"tokens_out_final":5,"usage_complete":true,"tokens_in_last":12,"tokens_out_last":5}
```

`latency_s` is the diagnostic duration of the **last** provider invocation, not the sum of retries. For batched embeddings, the last upstream call's duration is distributed among texts in proportion to their tokens (or equally if token counts are unknown or zero). In run files and results, the only time value is `wallclock_s`, measured by the code running the whole query. The log does not contain that time automatically. See [API.md](API.md) for the full contract.

## 5. Configure routes, providers, and prices

- `gateway.toml` contains the port, cache and log paths, retries, limits, price list, Google configuration, and B2 destination.
- `models.toml` maps a public name to `backend`, `task`, `upstream_model`, and, where needed, `base_url`, `api_key_env`, or schema options. To add a model from the same provider, add a route and call it by its public name. Existing cloud routes can remain in the file without credentials.
- The price list at `[pricing].prices_path` estimates costs in logs; it is not necessarily the provider's actual bill. The initial `prices/test_prices.toml` contains indicative prices for cloud routes and zero prices for local models: update rates before relying on cost estimates. A model without a price may have `cost_usd: null`.

For an Azure route, set the environment variable named by `api_key_env` in `models.toml`, or put it in `azure.local.env` (loaded by `start server` when present). Azure URLs copied from `models.toml.example` are placeholders: replace them with endpoints and deployments accessible to the group. Do not put secrets in `models.toml` or `gateway.toml`, and do not commit `*.local.env` files. Real calls to cloud providers may incur costs.

### Google Vertex AI

The `google_genai` adapter uses the `google-genai` SDK with `vertexai=True`, a **Google Cloud project and location**: it calls Vertex AI, not the Gemini Developer API with an API key. In your Google Cloud project, verify that billing and the Vertex AI API are enabled and that your identity has the necessary permissions. Set your `project_id` in `gateway.toml`, for example:

```toml
[google]
project_id = "YOUR_PROJECT_ID"
location = "global"
```

The `gemini-3.5-flash` route in `models.toml` selects the model for `/v1/responses`; `google/gemini-embedding-001` selects the model for `/v1/embeddings` and specifies `location = "europe-west4"`, which overrides the general location. A route in the TOML does not prove that the model and region are available to your project: check the project's Vertex AI catalog. The gateway does not require `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, or `GOOGLE_GENAI_USE_VERTEXAI`: the project, location, and Vertex AI selection come from the TOML and code.

The expected authentication method is [Application Default Credentials](https://docs.cloud.google.com/docs/authentication/application-default-credentials). On a student's computer, one option is `gcloud auth application-default login`; alternatively, configure an authorized credentials file through `GOOGLE_APPLICATION_CREDENTIALS`. For the latter, export the variable in the shell **before** `start server`, or create `google.local.env` in the gateway root:

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/credentials.json
```

`start server` reads that file if it exists and gives precedence to variables already exported in the environment. Do not upload the credentials file or `google.local.env` to the repository. To check without inference, run `doctor` and `start status`, then, if you use `gcloud`, run `gcloud auth application-default print-access-token >/dev/null`: this checks access to credentials, **not** authorization for a specific Vertex AI model. The gateway has no entitlement test that avoids a model call. For requirements and availability, see the [Vertex AI documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstarts/quickstart-multimodal) and [ADC documentation](https://docs.cloud.google.com/docs/authentication/application-default-credentials).

The course indicates roughly **€50 in Google Education credits** per student: check the terms, expiration, and whether the credits cover Vertex AI usage in your Billing account. The gateway provides no guarantee. Google Cloud budgets and alerts can be configured in the project; ordinary [budget alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets) do not automatically stop spending.

### Limits and accounting

`gateway.toml` configures maximum body sizes, maximum output tokens, request rate, concurrency, maximum wait, and `guardrails.run_limits.default_max_attempts_per_model` (initially 10,000 attempts). These are **local** limits for each instance: the count for `(run_id, model)` is kept in memory and resets when the server restarts. The gateway has no spending limit in euros/dollars or cumulative budget across group members' gateways. Provider quotas and limits are separate and must be configured with the provider.

`cost_usd` in the log is an **estimate** calculated from usage reported by the provider and the local price list; it is not an invoice. Failed attempts or attempts with unknown usage may still have an unobserved real cost. A cache hit does not call the provider again, but the log includes the original entry's usage and recalculates its **theoretical** cost with the current price list. In run files, the checker distinguishes the cost attributed to calls made during the run from the plan cost that includes the theoretical value of reused responses; neither replaces provider billing. Use `cache_hit`, `usage_complete`, and the price list identified by `price_list_id` to interpret these figures.

Neo4j and GraphDB are installed separately. Their code must send HTTP requests to the gateway. For the Neo4j GenAI plugin, set `genai.openai.baseurl=http://127.0.0.1:8000/v1` in `genai.conf` and pass `instructions`, `run_id`, and `cache_namespace` through the plugin options. Check behavior with the Neo4j version used by the group. Cypher exposes the structured content but not `parse_ok` or `error_type`: consult the log by `run_id` to distinguish a Boolean fallback from an actual `false`.

## 6. Synchronize the group cache manually

The cache works locally without B2. To share entries with classmates, each student sets `enabled = true` and the group's shared destination in `[cache_sync]` in `gateway.toml`, then fills **on their own computer** the `b2.local.env` file:

```dotenv
SEMOPS_STUDENT_ID=unique-student-id
B2_ACCESS_KEY_ID=group-key
B2_SECRET_ACCESS_KEY=group-secret
```

Group members share the account/bucket, endpoint, and `compatibility_id`, but have different `SEMOPS_STUDENT_ID` values. They must also agree on public names, upstream models, model versions, route configuration, and cache namespaces. `compatibility_id` is a prefix for remote objects and protects against different destinations; it **does not** automatically verify matching models or configurations. The canonical key incorporates the request and some route parameters, but neither `base_url` nor the actual revision of a model's weights: divergent configurations may therefore reuse an entry with the same key incorrectly. Use a distinct namespace for incompatible experiments and, if the B2 destination associated with a SQLite cache changes, use a distinct local cache.

After `source ./activate.sh`:

```sh
cache pull    # before work: import the group's new entries
cache status  # check local status
cache push    # after generating new responses: publish them
```

The gateway **does not synchronize automatically** at startup or during HTTP requests. The server does not need to be running to use these commands. `cache pull` and `cache push` do not invoke each other. `cache status` only reads local state, but still requires complete B2 configuration. Pull before starting and after others push; push after producing new responses. Different archives accumulate without overwriting one another, and entries with different keys are preserved. If two students generate different rows for the same type, namespace, and key, pull keeps the local row and records a conflict: the two instances may continue returning different responses, without automatic resolution. Only valid cache entries are synchronized, not fallbacks, logs, or run files. See [CACHE_B2.md](CACHE_B2.md) for operational details and limits.

## 7. Tests and run files

To run automated tests without cloud credentials:

```sh
uv run --locked pytest -q
```

Tests use simulated providers. The gateway does not produce complete run files by itself: students must run the query, record `started_at`, `finished_at`, and `wallclock_s`, preserve the final answer in **SPARQL 1.1 Query Results JSON**, and associate gateway calls with the same `run_id` (and correct `cache_namespace`). The canonical cache key identifies a request/model, not a run: the same `run_id` may contain misses and hits. In `semops-run/1` run files, `calls` comes from filtered JSONL rows for the run and includes only fields allowed by the specification; `error_type`, `latency_s`, secrets, and prompts must not be copied into `calls` records. The checker derives `parse_failures`, among other fields, from distinct `responses` keys with `parse_ok: false`; a successful internal retry does not count as a final fallback. Use the `semops-run/1` specification supplied with the checker.

The `runs` wrapper looks for `semops_check.py` at the **gateway root**. The checker is supplied separately from the gateway: place it there before using these commands. To check existing run files:

```sh
runs check
```

`runs check` invokes the checker on the repository root and writes `results.csv` with one row per usable run. Run files must already be in the `runs/` paths expected by the checker, along with supporting files required by the course specification.

`runs build` accepts:

```sh
runs build --manifest /path/to/manifest.json --answer /path/to/answer.json
```

The manifest must contain the actual run metadata: group, domain, dataset, query, run, and plan identifiers; `engine`, `model_oracle`, `language_expression`, `n_input_bindings`, `started_at`, `finished_at`, and `wallclock_s`. Optional or plan-dependent fields are defined by the checker's `RUN_FIELDS` schema; the checker adds retry counts from the log. `answer.json` must contain the SPARQL JSON answer already produced by student code. The checker reads the configured log and price list and creates `runs/<domain>/<data_set>/<query_id>/<run_id>.json`. It also accepts `--candidates`, `--oracle-check`, `--log-path`, `--prices-path`, and `--overwrite`. Student code remains responsible for the metadata and query result. Use metadata and results from your own execution.

## 8. Common problems

| Symptom | Check |
| --- | --- |
| `uv` not found | Install `uv`, then rerun `bash setup.sh`. |
| `.venv` missing | Run `bash setup.sh` from the repository root. |
| `start` not found | Run `source ./activate.sh` in the current terminal or `bin/start server` from the root. |
| `/health` does not respond | Check that `start server` is still running and check the host/port in `gateway.toml`. |
| `/health` works but Ollama does not | Check that Ollama is running, the model has been downloaded, and `base_url` in `models.toml` is correct. |
| HTTP 404 for a model | Use exactly a public name present in `models.toml`. |
| HTTP 422 | Check required fields, `metadata`, JSON Schema, and the route's task. |
| HTTP 429 or 502 | Check limits, retries, the provider, and `data/calls.jsonl`; do not automatically interpret this as a parse error. |
| `cache` reports missing credentials | Create `b2.local.env` and check `[cache_sync]`; if you do not use B2, skip `cache` commands. |
| `runs` reports a missing checker | Place the separately distributed `semops_check.py` file at the repository root. |
| Google returns an error although `/health` works | Check `project_id`, ADC, API, permissions, location, and model availability in your project; `doctor` does not check these. |
