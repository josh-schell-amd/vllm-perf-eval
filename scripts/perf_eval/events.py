"""Event timestamps, nightly identity, and the JSONL file the collector
writes for `aggregate.py` within one run."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

RESULT_EVENTS = frozenset({"perf_result", "accuracy_result"})
EXPECTED_CONFIGS_EVENT = "expected_configs"


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_time(value: object) -> datetime | None:
    """A UTC datetime from an ISO timestamp or date string, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def finished_at(result: dict) -> datetime | None:
    """When a result's nightly finished: its `date`."""
    return parse_time(result.get("date"))


def received_at(event: dict) -> datetime | None:
    """When the collector recorded an event: its `received_at`."""
    return parse_time(event.get("received_at"))


# ---------------------------------------------------------------------------
# Nightly identity
# ---------------------------------------------------------------------------


def nightly_identity(event: dict) -> str:
    """Day and vLLM commit, so a nightly's perf and accuracy results share it."""
    day = str(event.get("nightly_date") or "").strip()
    commit = str(event.get("vllm_commit") or "").strip()
    if day or commit:
        return f"nightly:{day}:{commit}"
    return f"build:{event.get('build_number')}"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def encoded_events(events: list[dict]) -> bytes:
    return "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
    ).encode("utf-8")


def encoded_json(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    """Write to a temp file beside path, then rename it over path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates the file owner-only (0600); give the result normal
        # read permissions (0644), since it replaces the real file.
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_events_atomic(path: Path, events: list[dict]) -> int:
    """Replace `path` with these events; returns how many."""
    _write_atomic(path, encoded_events(events))
    return len(events)


def write_json_atomic(path: Path, payload: dict) -> None:
    _write_atomic(path, encoded_json(payload))


def read_events_strict(path: Path) -> list[dict]:
    """Read a JSONL event file, failing on any malformed line."""
    if not path.exists():
        return []
    out: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid perf-eval JSONL at {path}:{number}: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise ValueError(
                f"invalid perf-eval JSONL at {path}:{number}: event must be a JSON object"
            )
        out.append(event)
    return out
