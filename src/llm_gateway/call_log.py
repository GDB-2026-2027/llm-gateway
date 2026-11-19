import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class CallRecord:
    run_id: str
    endpoint: Literal["responses", "embeddings"]
    cache_namespace: str

    model: str
    key: str

    cache_hit: bool
    parse_ok: bool | None

    # Known usage across all provider attempts.
    tokens_in: int | None
    tokens_out: int | None

    latency_s: float | None

    # Recomputed from the current price list, including on cache hits.
    cost_usd: float | None
    price_list_id: str | None

    attempts: int = 0
    parse_retries: int = 0
    other_retries: int = 0
    ts: float = field(default_factory=time.time)
    error_type: str | None = None
    # Failure diagnostics must not contain request content or secrets.
    error_detail: str | None = None

    # Usage of the final valid generation, if one exists.
    tokens_in_final: int | None = None
    tokens_out_final: int | None = None

    # False if any attempt may have unreported usage.
    usage_complete: bool = False

    # Usage of the last generation, even when its JSON is invalid.
    tokens_in_last: int | None = None
    tokens_out_last: int | None = None


class JsonlCallLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._lock = threading.Lock()

    def write(self, record: CallRecord) -> None:
        """Append one gateway invocation to the JSONL log."""

        line = json.dumps(
            asdict(record),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

        with (
            self._lock,
            self.path.open(
                "a",
                encoding="utf-8",
            ) as file,
        ):
            file.write(line + "\n")
