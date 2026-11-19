# Gateway HTTP contract

The local server exposes `GET /health`, `POST /v1/responses`, and `POST /v1/embeddings` (usually at `http://127.0.0.1:8000`). Public model names and their tasks are defined in `models.toml`. Endpoints accept JSON and reject bodies with unknown fields. This API is **specific to this project**: the name `/v1/responses` does not imply full compatibility with OpenAI's Responses API.

## `POST /v1/responses`

Required body fields:

| Field | Type and constraints |
| --- | --- |
| `model` | Nonempty string; public name of a route with the `responses` task. |
| `instructions` | Nonempty string with instructions for the model. |
| `input` | Nonempty list of messages `{ "role": "system" | "user" | "assistant", "content": "..." }`; each `content` is nonempty. |
| `text.format` | Object with `type: "json_schema"`, a nonempty `name`, `strict: true`, and `schema` (valid JSON Schema Draft 2020-12). |
| `metadata` | Object with `run_id` and `cache_namespace`, both nonempty strings. |

Optional fields: `temperature` (a number between `0` and `2`, or `null`) and `top_logprobs` (a nonnegative integer, or `null`). Per-request streaming, tool calls, and `max_output_tokens` are not supported: the output limit is configured in `gateway.toml`.

Example using the local `local/qwen2.5:3b` route; requires Ollama and the model to be available:

```sh
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/qwen2.5:3b",
  "instructions": "Respond only in JSON.",
  "input": [{"role": "user", "content": "2 + 2 = 4?"}],
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
  "metadata": {"run_id": "test-1", "cache_namespace": "group-1"}
}
JSON
```

A valid HTTP 200 response has this envelope. The value of `output[0].content[0].text` is **a string containing JSON** and must be parsed again. Here `false` is an actual negative decision because `parse_ok` is `true`:

```json
{
  "output": [{"type": "message", "content": [{"type": "output_text", "text": "{\"answer\":false}"}]}],
  "parse_ok": true,
  "provider_response": {"id": "example-provider"}
}
```

`provider_response` contains the raw object returned by the provider, so its shape and any usage or log-probability fields depend on the backend. The `/v1/responses` response has no normalized field for tokens, costs, or log probabilities; these are recorded in the log when available.

### Invalid output and fallback

The gateway parses the model text as JSON and checks it against the requested schema. If validation fails, it retries generation according to `retries.invalid_output` in `gateway.toml`. If a retry produces valid output, the final response has `parse_ok: true`; the log retains the number of `parse_retries`.

When all attempts produce invalid output and the schema supports a fallback, the gateway still returns **HTTP 200**:

```json
{
  "output": [{"type": "message", "content": [{"type": "output_text", "text": "{\"answer\":false}"}]}],
  "parse_ok": false,
  "provider_response": {"id": "last-invalid-generation"},
  "error_type": "parse_error"
}
```

`parse_ok` and `error_type` are outside the JSON string in `text`; they are not added to `{"answer":false}`. A direct HTTP client must read `parse_ok` rather than infer success from the Boolean value alone. `provider_response` refers to the provider's last attempt, not the fallback.

For classification with a `{ "label": { "type": "string", "enum": [...] } }` schema, the fallback uses a reserved label based on `__PARSE_FAILURE__`. If that string is already in the enum, a `_` prefix is added until there is no collision. The fallback label is deliberately outside the valid classes and may not satisfy the requested enum. For other schemas without a fallback, the gateway returns HTTP 502 (`No fallback available`) after retries and records `parse_ok: false` with `error_type: "unsupported_fallback"`.

Valid generations are stored in the SQLite cache, including usage, retry information, and the raw provider response. Fallbacks are not stored. On a cache hit, the client receives `parse_ok: true` again, along with the same `output` and stored raw response. The cache-hit log includes the original call's `attempts`, `parse_retries`, and `other_retries`: for `responses`, `other_retries` is derived from `attempts - 1 - parse_retries`; for embeddings, from `attempts - 1` on the entry representing the upstream call. `cache_hit: true` indicates that those retries did not happen during the new request. There is no `cache_hit` indicator in the HTTP body; it is in the log. Caches are separated by `cache_namespace`; `run_id` identifies the run for logging and attempt limits but is not part of the cache key. Keys depend on the model, messages, schema, and configured parameters.

### Log probabilities

A route can declare `top_logprobs` in `models.toml`. A request asking for more than the configured capacity receives HTTP 422. When the route has a positive value, the adapter requests the depth **configured on the route**, even if the client omits the field or asks for a lower value. The gateway does not normalize log probabilities: if the provider returns them, they appear in `provider_response`, including on cache hits. The `microsoft_decision` backend does not support token log probabilities. Actual availability depends on the provider and model.

## `POST /v1/embeddings`

Required fields: `model` (a nonempty string naming a route with the `embeddings` task), `input` (a nonempty string or a nonempty list of nonempty strings), and `metadata` with `run_id` and `cache_namespace` as above. Optional field: `encoding_format`, whose only allowed value is `"float"` (the default). Unknown fields are rejected.

```sh
curl -sS http://127.0.0.1:8000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/nomic-embed-text",
  "input": ["first text", "second text"],
  "metadata": {"run_id": "test-1", "cache_namespace": "group-1"}
}
JSON
```

With the corresponding Ollama model available, the response has this shape (vectors and counts are illustrative):

```json
{
  "object": "list",
  "model": "local/nomic-embed-text",
  "data": [
    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
    {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]}
  ],
  "usage": {"prompt_tokens": 4, "total_tokens": 4}
}
```

`data` preserves input order and indices. Identical texts in one request are deduplicated for upstream calls, but each position retains its own `data` element and log row. The SQLite cache is per text and namespace. `usage` is present only if the gateway knows the token counts of new calls made for **this** request; cache hits are not counted. It may therefore be absent or show zero when everything comes from the cache. Google GenAI accepts at most `guardrails.max_google_embedding_texts` texts per request and sends each missing text separately.

## Errors and logs

| HTTP | Main cases |
| --- | --- |
| 200 | Valid response, cache hit, or supported fallback after a parse error; check `parse_ok` for `/v1/responses`. |
| 413 | Body exceeds the configured limit (`max_body_bytes` for responses, `max_embeddings_body_bytes` for embeddings). |
| 404 | Unknown public model name. |
| 422 | Invalid body or JSON Schema, wrong task, or an option not supported by the route. |
| 429 | Local gateway rate/concurrency limit or exhausted attempts for `(run_id, model)`. An upstream HTTP 429 may become 502 after retries. |
| 501 | Backend not implemented for the requested task. |
| 502 | Upstream/transport error, malformed upstream response, or unavailable fallback. |
| 503 | Corrupt cache entry; the gateway does not make a new upstream call. |

HTTP errors generally have a `{"detail": ...}` body. Transport errors, timeouts, and budget errors do not become `parse_error`: they have `parse_ok: null` and a distinct `error_type` in the logs. The JSONL file configured in `[logging]` contains one row per `/v1/responses` call that reaches the cache/provider phase and one row per input position in a completed `/v1/embeddings` batch; requests rejected earlier may have no row. The `run_id`, `cache_namespace`, `model`, `key`, `cache_hit`, `parse_ok`, `parse_retries`, `other_retries`, `attempts`, `error_type`, usage, `latency_s`, and cost fields allow clients to identify fallbacks and count retries actually performed. For responses, `latency_s` is the duration of the last provider invocation. For embedding batches, it is the share of the last invocation's duration assigned to each text in proportion to its tokens, as with cost; when token counts are unknown or total zero, duration is split equally. This value is retained for diagnostics in the cache and log; a cache hit copies the original share. Existing cache rows retain the latency value recorded when they were created. A fallback records `parse_ok: false` and `error_type: "parse_error"`. The external checker exports a fallback as a `calls` record without `error_type` or `latency_s`, accepts multiple invocations of the same key with different outcomes, and derives `n_parse_retries` and `n_other_retries` from logs. The only time value in run files and results is `run.wallclock_s`. The `tokens_in_final`/`tokens_out_final` and `tokens_in_last`/`tokens_out_last` fields identify terminal-attempt tokens for the run file; earlier retries are not summed into the main metrics.

## Database bindings

For Neo4j GenAI, point `genai.openai.baseurl` at the local gateway and pass `instructions`, `metadata.run_id`, and `metadata.cache_namespace` through `vendorOptions`. The plugin requires `output[0].type: "message"`. Cypher returns the validated map, for example `{answer: false}` or `{label: "__PARSE_FAILURE__"}`; `parse_ok` and `error_type` are not exposed in the map. For Boolean results, the JSONL log identified by `run_id` distinguishes a fallback from an actual negative decision. A direct HTTP client can read `parse_ok` in the response body instead.

If Neo4j runs in Docker while the gateway runs on the host, `127.0.0.1` inside the container refers to the container. Use a host address reachable from the container (often `host.docker.internal` on Docker Desktop), make `[server].host` listen on a reachable interface, and limit access to the intended network.

For RDFLib and Oxigraph, SPARQL functions must make HTTP requests to the gateway. The repository does not include students' operators.
