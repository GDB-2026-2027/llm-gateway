# Run files and the external checker

The gateway writes call records to the JSONL path configured by `[logging].log_path`; it does not produce complete experiment run files by itself. The checker and the `semops-run/1` specification are supplied separately. **Keep `semops_check.py` outside the gateway repository.** The `runs` wrapper executes the external file named by `SEMOPS_CHECKER_PATH`. Check that file's `RUN_FIELDS` schema for the exact required and optional manifest fields.

In each terminal where you use `runs`, set the path to the separately supplied checker:

```sh
export SEMOPS_CHECKER_PATH=/absolute/path/to/semops_check.py
```

A relative path is resolved against the terminal's current directory before the wrapper changes to the gateway root. The wrapper rejects a checker path inside the gateway repository. It runs the checker with the gateway's Python environment, using the gateway root for run files, configuration, logs, and `results.csv`. You can also invoke the external checker directly with Python and supply the repository root yourself.

If your own query code imports helper functions from `semops_check.py`, make its external directory importable in that code's Python environment (for example through `PYTHONPATH` or your project's package setup). The gateway server does not import the checker.

Student query code must record the actual `started_at`, `finished_at`, and `wallclock_s` for the whole query, preserve the final answer in **SPARQL 1.1 Query Results JSON**, and use a recognizable `metadata.run_id` and appropriate `metadata.cache_namespace` in gateway requests. The same `run_id` can include cache misses and hits; `run_id` is not part of the cache key.

To check run files already placed in the paths expected by the checker:

```sh
runs check
```

This invokes the external checker on the gateway repository root and writes `results.csv` there with one row per usable run. Run files and any supporting files must already exist in the `runs/` paths required by the separate specification. Extra checker options such as `--data`, `--keys`, and `--prices` can follow `runs check`.

To build a run file from the result and metadata produced by your code:

```sh
runs build --manifest /path/to/manifest.json --answer /path/to/answer.json
```

The manifest records real group, domain, dataset, query, run, and plan identifiers, together with fields such as `engine`, `model_oracle`, `language_expression`, `n_input_bindings`, `started_at`, `finished_at`, and `wallclock_s`. `answer.json` must contain the actual SPARQL JSON answer. The checker reads the configured log and price list and creates a file under `runs/<domain>/<data_set>/<query_id>/<run_id>.json`. The wrapper also passes through checker options such as `--candidates`, `--oracle-check`, `--log-path`, `--prices-path`, and `--overwrite`. Use absolute paths for these input files unless they are under the gateway root, because the wrapper runs with that root as its working directory.

The `calls` records in `semops-run/1` contain only fields allowed by that specification. Do not copy secrets, prompts, `error_type`, or diagnostic `latency_s` into them. The checker derives retry counts from gateway logs. A successful internal parse retry is not a final fallback; the checker identifies parse failures from distinct response keys with `parse_ok: false`. The main time value in run files and results is the query's `wallclock_s`, not the last provider call's `latency_s`. See [API.md](API.md) for log fields and [CONFIGURATION.md](CONFIGURATION.md) for price-list settings.
