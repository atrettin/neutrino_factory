"""Record of the retry scripts a run has issued, to stop two arrays sharing a task.

``diagnosis`` works from files only, so a task that a retry has queued but not
started yet still looks failed. A second ``retry`` would select it again, and the
two array tasks would later write to the same chunk directory at the same time,
truncating each other's files. The ledger remembers which task indices each retry
script covers and when it was issued; a task counts as still pending until its
sidecar shows a start after that time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .diagnosis import _parse_utc


def ledger_path(config: dict[str, Any]) -> Path:
    work_root = Path(config["storage"]["work_root"])
    return work_root / "slurm" / f"{config['run']['name']}_retry_ledger.json"


def load_ledger(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = ledger_path(config)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def record_retry(
    config: dict[str, Any],
    indices: Iterable[int],
    script_path: Path,
    modes: list[str],
    submitted: bool,
    issued: datetime,
) -> None:
    entries = load_ledger(config)
    entries.append(
        {
            "issued_utc": issued.isoformat(),
            "script": str(script_path),
            "modes": modes,
            "submitted": submitted,
            "task_indices": sorted(int(i) for i in indices),
        }
    )
    path = ledger_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1), encoding="utf-8")


def pending_retries(
    config: dict[str, Any], diagnoses: Iterable[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """Map each given task to the earlier retry that covers it and has not started it.

    A task whose sidecar start is at or after the retry's issue time has been
    started by it (or later), so a failure now is a new one and may be retried.
    """
    entries = load_ledger(config)
    pending: dict[int, dict[str, Any]] = {}
    for diagnosis in diagnoses:
        index = int(diagnosis["task_index"])
        start = _parse_utc(diagnosis.get("start_utc"))
        for entry in entries:
            if index not in entry["task_indices"]:
                continue
            issued = datetime.fromisoformat(entry["issued_utc"])
            if start is None or start < issued:
                pending[index] = entry
    return pending


def new_script_path(config: dict[str, Any], modes: list[str], issued: datetime) -> Path:
    """A path no earlier retry used: the retry script is never overwritten."""
    stamp = issued.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    label = "-".join(modes) if len(modes) <= 3 else f"{len(modes)}modes"
    directory = Path(config["storage"]["work_root"]) / "slurm"
    base = f"{config['run']['name']}_retry_{label}_{stamp}"
    path = directory / f"{base}.sbatch"
    counter = 1
    while path.exists():
        path = directory / f"{base}_{counter}.sbatch"
        counter += 1
    return path
