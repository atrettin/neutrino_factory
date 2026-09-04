"""Per-chunk run sidecars and per-job status statistics.

A chunk's progress cannot be told from its HDF5 output alone: before the
generator finishes there is no file to look at, and a finished file carries no
wall-clock timing. So ``local.run_task`` writes a small JSON **sidecar** beside
each normalized chunk file when the task starts, recording the start time and
the task's identity, and updates it when the task finishes with the stop time
and whether the output validated. The functions here write and read those
sidecars and roll them up into per-job statistics for ``check-status``.

Sidecars are plain dictionaries so the diagnostic functions never print; the
``format_*`` helper turns a stats row into table text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from .layout import chunk_sidecar_path

#: Sidecar keys set when the task starts running.
START_KEYS = (
    "run_name",
    "job_label",
    "task_index",
    "chunk_id",
    "generator",
    "code_version",
    "config_version",
    "seed",
    "event_count",
    "execution_mode",
    "start_utc",
    "status",
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string, as recorded in sidecars."""
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(start_utc: str, stop_utc: str) -> float:
    try:
        start = datetime.fromisoformat(start_utc)
        stop = datetime.fromisoformat(stop_utc)
    except ValueError:
        return 0.0
    return max(0.0, (stop - start).total_seconds())


def write_sidecar_start(config: Mapping[str, Any], task: Mapping[str, Any]) -> Path:
    """Write the "running" sidecar for ``task``, returning its path.

    The start time is captured here, before the generator runs, so it reflects
    when the task actually began rather than when it produced output.
    """
    path = chunk_sidecar_path(config, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_name": str(task["run_name"]),
        "job_label": str(task["job_label"]),
        "task_index": int(task["task_index"]),
        "chunk_id": int(task["chunk_id"]),
        "generator": str(task["generator_name"]),
        "code_version": str(task["code_version"]),
        "config_version": str(task["config_version"]),
        "seed": int(task["seed"]),
        "event_count": int(task["event_count"]),
        "execution_mode": str(config.get("run", {}).get("executor", "local")),
        "start_utc": utc_now_iso(),
        "status": "running",
    }
    _atomic_write(path, payload)
    return path


def finish_sidecar(path: str | Path, valid: bool, status: str = "finished") -> Path:
    """Record a task's completion on its sidecar and return the path.

    Adds the stop time, wall-clock duration and output validity. A task that
    raised records ``valid=False`` and ``status="failed"`` so a run's failures
    are visible even when no HDF5 was produced.
    """
    path = Path(path)
    payload = read_sidecar(path) or {}
    payload["stop_utc"] = utc_now_iso()
    payload["status"] = status
    payload["valid"] = bool(valid)
    if "start_utc" in payload:
        payload["duration_sec"] = _seconds_since(str(payload["start_utc"]), str(payload["stop_utc"]))
    _atomic_write(path, payload)
    return path


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON, atomically, so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_sidecar(path: str | Path) -> dict[str, Any] | None:
    """Read a sidecar file, or ``None`` if it does not exist or is unreadable."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def job_status_stats(chunks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll one job's chunk entries (from ``expected_outputs``) into status stats.

    ``chunks`` entries carry the sidecar ``path`` (via ``expected_outputs``);
    chunks without a sidecar simply count as neither started nor finished. The
    returned dict has ``total``/``started``/``finished``/``valid`` counts, the
    earliest start and latest finish times, and the average/maximum chunk wall-
    clock duration over finished chunks (``None`` when none is known).
    """
    total = len(chunks)
    started = 0
    finished = 0
    valid = 0
    first_start: str | None = None
    last_finish: str | None = None
    durations: list[float] = []

    for chunk in chunks:
        sidecar = read_sidecar(chunk["sidecar_path"])
        if sidecar is None:
            continue

        start_utc = sidecar.get("start_utc")
        if start_utc:
            started += 1
            if first_start is None or str(start_utc) < first_start:
                first_start = str(start_utc)

        stop_utc = sidecar.get("stop_utc")
        if stop_utc or sidecar.get("status") == "finished":
            finished += 1
            if stop_utc:
                if last_finish is None or str(stop_utc) > last_finish:
                    last_finish = str(stop_utc)
                if start_utc:
                    durations.append(_seconds_since(str(start_utc), str(stop_utc)))

        if sidecar.get("valid"):
            valid += 1

    return {
        "total": total,
        "started": started,
        "finished": finished,
        "valid": valid,
        "first_start_utc": first_start,
        "last_finish_utc": last_finish,
        "avg_duration_sec": (sum(durations) / len(durations)) if durations else None,
        "max_duration_sec": max(durations) if durations else None,
    }


def format_status_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render ``job_status_stats`` rows as an aligned plain-text table.

    Each row must carry ``job_label`` plus the keys of :func:`job_status_stats`.
    """
    if not rows:
        return "(no jobs)"

    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.1f}"
        return str(value)

    header: tuple[str, ...] = (
        "JOB",
        "CHUNKS",
        "STARTED",
        "FINISHED",
        "VALID",
        "FIRST_START",
        "LAST_FINISH",
        "AVG_S",
        "MAX_S",
    )
    lines: list[tuple[str, ...]] = [header]
    for row in rows:
        stats = row
        lines.append(
            (
                str(stats["job_label"]),
                cell(stats["total"]),
                cell(stats["started"]),
                cell(stats["finished"]),
                cell(stats["valid"]),
                cell(stats["first_start_utc"]),
                cell(stats["last_finish_utc"]),
                cell(stats["avg_duration_sec"]),
                cell(stats["max_duration_sec"]),
            )
        )

    widths = [max(len(line[col]) for line in lines) for col in range(len(header))]
    rendered = ["  ".join(value.ljust(widths[col]) for col, value in enumerate(line)) for line in lines]
    return "\n".join(rendered)
