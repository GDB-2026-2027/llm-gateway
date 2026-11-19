# LLM Gateway

A local HTTP gateway for the university project on semantic operators. Each student runs their own instance; model calls go through `/v1/responses` or `/v1/embeddings`. Responses are stored in SQLite and calls are recorded in JSONL. The group's cache on Backblaze B2 is synchronized only through `cache pull` and `cache push`.

To get started from a fresh clone, follow the [gateway operating guide](docs/GATEWAY_GUIDE.md).

## Requirements and installation

- `uv` installed; `setup.sh` selects Python 3.12 (`uv` can download it if necessary).
- For Ollama models, a local Ollama server and the models listed in `models.toml`.
- Cloud and B2 credentials are needed only for their respective features.

From a clean repository copy, `setup.sh` creates `gateway.toml` and `models.toml` from the corresponding `.example` files without overwriting existing local configurations:

```sh
bash setup.sh
source ./activate.sh
doctor
```

`bash setup.sh` is the only command needed to install the gateway: it runs `uv sync --locked --python 3.12` and installs the package, configurable providers, B2 synchronization, and `pytest` in `.venv`. **It does not install Neo4j, GraphDB/RDF4J, RDFLib, or PyOxiGraph.** Neo4j and GraphDB are external servers that each group installs and configures independently. To select another compatible Python version: `SEMOPS_PYTHON_VERSION=3.13 bash setup.sh`. The commands in `bin/` use `.venv/bin/python` directly; setting `PYTHONPATH` is unnecessary.

Students who use Python RDF bindings can add them separately (they are optional and are not needed to call the gateway's HTTP APIs):

```sh
uv sync --locked --group rdflib       # RDFLib only
uv sync --locked --group oxigraph     # PyOxiGraph only
uv sync --locked --group rdflib --group oxigraph  # both
```

`uv sync` synchronizes the environment exactly: running `bash setup.sh` again removes optional groups unless they are selected again. Likewise, invoke `uv run` with the optional groups needed by the script, for example `uv run --locked --group rdflib python script.py`. `source ./activate.sh` adds `start`, `cache`, `doctor`, and `runs` to `PATH`, but does not install packages.

## Configuration and startup

`gateway.toml` contains the port, cache, log, limits, retries, and provider settings. `models.toml` maps public model names to backends, upstream models, and options. Edit the local copies for your environment; the `.example` files remain shared. Cloud routes can stay in the file without credentials: a local model can be used without configuring Google or Azure. For Google GenAI, set `google.project_id` in `gateway.toml` and provide the Google credentials required by your environment. For Azure, fill `azure.local.env` with the variable named by `api_key_env` in the route, or export it in the environment. `start server` loads the Azure file if present and also loads `google.local.env` if present. Git ignores `*.local.env` files.

```sh
start server
```

In a second terminal:

```sh
source ./activate.sh
start status
curl http://127.0.0.1:8000/health
```

The expected `/health` response is `{"status":"ok"}`. Request schemas are also available at `http://127.0.0.1:8000/docs`. HTTP requests require `metadata.run_id` and `metadata.cache_namespace`.

The implemented contract, including fallbacks and error codes, is documented in [docs/API.md](docs/API.md).

Example HTTP call to a local model after starting Ollama and downloading `qwen2.5:3b`:

```sh
curl -X POST http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"local/qwen2.5:3b","instructions":"Respond in JSON","input":[{"role":"user","content":"2 + 2 = 4?"}],"text":{"format":{"type":"json_schema","name":"answer","strict":true,"schema":{"type":"object","properties":{"answer":{"type":"boolean"}},"required":["answer"],"additionalProperties":false}}},"metadata":{"run_id":"test-1","cache_namespace":"local"}}'
```

For embeddings, use the same `metadata` with `POST /v1/embeddings` and a body such as `{"model":"local/nomic-embed-text","input":["first text","second text"],"metadata":{"run_id":"test-1","cache_namespace":"local"}}` (requires the corresponding Ollama model).

## B2 cache and university tools

The local cache works without B2. To synchronize it manually, set `enabled = true` and the destination in `[cache_sync]` in `gateway.toml`, fill `b2.local.env` with `SEMOPS_STUDENT_ID`, `B2_ACCESS_KEY_ID`, and `B2_SECRET_ACCESS_KEY`, then use `cache status`, `cache pull`, or `cache push`. Server startup does not synchronize the cache.

The workflow between students and conflict handling are described in [docs/CACHE_B2.md](docs/CACHE_B2.md).

The `runs check` and `runs build` commands require the external `semops_check.py` file at the repository root. The checker is distributed separately from the gateway. The gateway CLI does not require RDFLib, PyOxiGraph, Neo4j, or GraphDB.

## Tests

```sh
uv run --locked pytest -q
```

The tests use simulated providers and temporary directories; they require no cloud credentials, Ollama, or B2. They do not verify the reachability or actual behavior of providers.
