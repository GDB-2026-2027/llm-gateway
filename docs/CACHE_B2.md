# Group cache on Backblaze B2

Each student runs a local gateway with a SQLite cache. Students in the same group share a B2 bucket/account to exchange **cache entries only**; JSONL logs and run files are not synchronized. The server does not access B2 automatically: transfers happen only through the manual commands described here.

## Configuration

The `[cache_sync]` section of `gateway.toml` contains `enabled = true`, the bucket, B2 endpoint, `compatibility_id`, `credentials_file` path, and archive size limits. Each student must create the indicated file locally (`b2.local.env` by default) with:

```dotenv
SEMOPS_STUDENT_ID=unique-student-id
B2_ACCESS_KEY_ID=shared-group-key
B2_SECRET_ACCESS_KEY=shared-group-secret
```

Do not commit this file. Students in the same group use the same bucket, endpoint, and `compatibility_id`, but each has a distinct `SEMOPS_STUDENT_ID`. Synchronization refuses to change the destination or student identity of a local database already associated with another destination. Cache table schemas must match across instances.

## Commands

After `source ./activate.sh`:

```sh
cache status
cache pull
cache push
```

All three commands accept `--config PATH` (default: `gateway.toml`). `cache status` shows counts of local, synchronized, queued, and new rows, known archives, and conflicts. It is a local check, but still requires the credentials file because the B2 configuration is validated when the command starts. `cache pull` lists and imports group archives that are not yet known. `cache push` publishes new local entries. Neither command runs the other automatically.

A practical workflow is `cache pull` before starting work and `cache push` afterward. Repeat `cache pull` after other students push to receive their updates. `cache status` alone does not modify remote objects.

## Concurrent updates and conflicts

Each push creates compressed archives with distinct B2 keys containing `compatibility_id`, `SEMOPS_STUDENT_ID`, and a UUID. A later push by another student does not overwrite the earlier archive. Subsequent pulls import missing entries: **updates with different cache keys are preserved**, even if students push without pulling first. Import verifies archive size and SHA-256.

When two instances have an entry with the same `(type, cache_namespace, key)` but different rows, pull **keeps the local row** and records a conflict in `cache status`; it does not replace the row automatically. The comparison covers the entire row, so differences in generated text, tokens, latency, or raw response can also cause conflicts. If both students have already pushed, both archives remain in the bucket, but local caches may continue returning different results. If one instance imports the other's row before generating that request, a cache hit reduces this possibility. Conflicting rows are excluded from selection for new local pushes; conflict resolution is not implemented.

A parse-error fallback is not stored in the response cache and therefore is not published to B2.
