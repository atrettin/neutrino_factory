"""Classify each task of a run by how it ended, from files readable in the container.

``check-status`` runs inside the container, where neither ``sacct`` nor
``squeue`` is available. Everything here is therefore read from files: the
chunk's sidecar and HDF5 output, the task's Slurm ``.out``/``.err`` logs and
the modification times in its raw work directory. Slurm itself writes a line
into a task's stderr when it kills the task (time limit, node failure,
``scancel``, out of memory), and that line is the strongest evidence there is.

Every rule needs positive evidence. A task with neither a log nor a sidecar is
reported as ``no_output``, not guessed to be a node failure: from inside the
container a task still waiting in the queue looks exactly the same.

The functions return plain dictionaries and never print; the ``format_*``
helpers render them as text.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import layout
from .status import read_sidecar

# Failure modes in the order ``check-status`` lists them. ``ok`` and
# ``running`` are healthy; everything else is a failure.
FAILURE_MODES = (
    "crash",
    "oom",
    "timeout",
    "cancelled",
    "node_failure",
    "hung",
    "invalid",
    "no_output",
)
HEALTHY_MODES = ("ok", "running")
ALL_MODES = (*HEALTHY_MODES, *FAILURE_MODES)

# Modes ``retry`` may relaunch. ``running`` and ``hung`` tasks still hold their
# Slurm slot and write to the chunk's paths, so a retry would run alongside
# them; a hung task has to be cancelled first, and then shows up as
# ``cancelled``.
RETRYABLE_MODES = ("no_output", "node_failure", "crash", "timeout", "oom", "cancelled", "invalid")

DEFAULT_HUNG_AFTER_HOURS = 4.0

NO_OUTPUT_NOTE = (
    "Tasks with no output may still be waiting in the queue, or died due to node failure."
)

# A task's sidecar may stay "running" this long past the Slurm time limit
# before it counts as timed out without Slurm's own message: the limit is
# enforced with some slack, and clocks differ slightly between nodes.
_TIME_LIMIT_GRACE = timedelta(minutes=10)

# Only the end of a log is read: Slurm's messages and the final error come last,
# and generator logs can run to many megabytes.
_TAIL_BYTES = 256 * 1024

_LOG_NAME = re.compile(r"^(?P<name>.+)_(?P<array>\d+)_(?P<index>\d+)\.(?P<ext>out|err)$")
# "slurmstepd: error: *** JOB 176457_217 ON et38 CANCELLED AT 2026-10-28T17:05:00
# DUE TO TIME LIMIT ***" (also "STEP ..." for the batch step).
_SLURM_KILL = re.compile(
    r"\*\*\* (?:JOB|STEP) (?P<job>\S+) ON (?P<node>\S+) CANCELLED AT (?P<at>\S+)"
    r"(?: DUE TO (?P<due>[A-Z][A-Z ]*?))? \*\*\*"
)
_OOM = re.compile(r"oom[-_]kill event", re.IGNORECASE)
_PY_EXCEPTION = re.compile(r"^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt)(?::|$)")
_GENERATOR_ERROR = re.compile(r"error|abort|fatal|exception|signal|segmentation", re.IGNORECASE)


def parse_slurm_time(value: Any) -> timedelta | None:
    """Parse a Slurm ``--time`` value (``MM``, ``MM:SS``, ``HH:MM:SS``, ``D-HH[:MM[:SS]]``)."""
    if value is None:
        return None
    text = str(value).strip()
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        if not day_text.isdigit():
            return None
        days = int(day_text)
        parts = text.split(":")
        if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
            return None
        hours, minutes, seconds = (list(map(int, parts)) + [0, 0])[:3]
    else:
        parts = text.split(":")
        if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
            return None
        numbers = list(map(int, parts))
        if len(numbers) == 1:
            hours, minutes, seconds = 0, numbers[0], 0
        elif len(numbers) == 2:
            hours, minutes, seconds = 0, numbers[0], numbers[1]
        else:
            hours, minutes, seconds = numbers
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def collapse_carriage_returns(text: str) -> list[str]:
    """Split ``text`` into lines as a terminal would show them.

    Progress counters rewrite one line with ``\\r``; only the last state of such
    a line is kept, so a log tail is not a wall of progress updates.
    """
    lines = []
    for raw in text.split("\n"):
        segments = [segment for segment in raw.split("\r") if segment.strip()]
        lines.append(segments[-1].rstrip() if segments else "")
    return lines


def read_tail(path: Path | None, max_bytes: int = _TAIL_BYTES) -> str:
    """The last ``max_bytes`` of a text file, or ``""`` if it is absent."""
    if path is None or not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def tail_lines(path: Path | None, count: int) -> list[str]:
    """The last ``count`` non-blank lines of a log, ``\\r``-collapsed."""
    lines = [line for line in collapse_carriage_returns(read_tail(path)) if line.strip()]
    return lines[-count:]


def find_task_logs(config: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Map each task index to its newest Slurm log pair.

    Log names follow the sbatch ``-o``/``-e`` pattern ``%x_%A_%a``, with job name
    ``nf_<run>`` for the original submission and ``nf_<run>_retry`` for a retry.
    Array job IDs grow with each submission, so the highest one is the latest
    attempt.
    """
    logs_dir = Path(config["storage"]["work_root"]) / "logs"
    job_names = {f"nf_{config['run']['name']}", f"nf_{config['run']['name']}_retry"}
    found: dict[int, dict[str, Any]] = {}
    if not logs_dir.is_dir():
        return found
    for path in logs_dir.iterdir():
        match = _LOG_NAME.match(path.name)
        if not match or match["name"] not in job_names:
            continue
        index, array_id = int(match["index"]), int(match["array"])
        entry = found.get(index)
        if entry is None or array_id > entry["array_job_id"]:
            entry = {"array_job_id": array_id, "out": None, "err": None}
            found[index] = entry
        if array_id == entry["array_job_id"]:
            entry[match["ext"]] = path
    return found


def array_job_ids(logs: Mapping[int, Mapping[str, Any]]) -> list[int]:
    """Every Slurm array job ID seen in a run's logs, oldest first."""
    return sorted({int(entry["array_job_id"]) for entry in logs.values()})


def _mtime(path: Path | None) -> datetime | None:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def newest_files(directory: Path, count: int) -> list[tuple[datetime, int, str]]:
    """The ``count`` most recently modified files under ``directory``."""
    entries: list[tuple[datetime, int, str]] = []
    if not directory.is_dir():
        return entries
    for path in directory.rglob("*"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            entries.append((mtime, stat.st_size, str(path.relative_to(directory))))
    entries.sort(reverse=True)
    return entries[:count]


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_CALLED_PROCESS = re.compile(
    r"CalledProcessError: Command '(?P<cmd>\[.*\])' (?P<outcome>died with .*?|returned non-zero exit status .*?)\.?$"
)


def _shorten_exception(line: str) -> str:
    """Put a failed subprocess's outcome (e.g. the signal) before its long command."""
    match = _CALLED_PROCESS.search(line)
    if not match:
        # "uproot.exceptions.KeyInFileError: ..." -> "KeyInFileError: ..."
        return re.sub(r"^(?:[A-Za-z_]\w*\.)+(?=[A-Za-z_]\w*(?::|$))", "", line)
    try:
        command = ast.literal_eval(match["cmd"])
    except (ValueError, SyntaxError):
        return line
    words = [str(word) for word in command]
    shown = " ".join(words[:4]) + (" ..." if len(words) > 4 else "")
    return f"{shown} {match['outcome']}"


def _failure_reason(err_text: str, out_text: str) -> str:
    """The last Python exception in stderr plus the last error-like generator line.

    Both are needed: when a generator aborts without a non-zero exit code, the
    Python traceback only shows the follow-on error (e.g. an empty ROOT file),
    while the real cause is the generator's own message in stdout.
    """
    exception = next(
        (line.strip() for line in reversed(collapse_carriage_returns(err_text))
         if _PY_EXCEPTION.match(line.strip())),
        "",
    )
    generator = next(
        (line.strip() for line in reversed(collapse_carriage_returns(out_text))
         if _GENERATOR_ERROR.search(line)),
        "",
    )
    parts = [_shorten_exception(exception)]
    if generator:
        parts.append(f"out: {generator}")
    return " | ".join(part for part in parts if part)


def _slurm_kill(err_text: str) -> dict[str, str] | None:
    matches = list(_SLURM_KILL.finditer(err_text))
    if not matches:
        return None
    last = matches[-1]
    return {
        "job": last["job"],
        "node": last["node"],
        "at": last["at"],
        "due": (last["due"] or "").strip(),
    }


def diagnose_task(
    config: Mapping[str, Any],
    task: Mapping[str, Any],
    logs: Mapping[str, Any] | None,
    now: datetime,
    hung_after: timedelta,
    time_limit: timedelta | None,
) -> dict[str, Any]:
    """Classify one task. See the module docstring for the evidence used."""
    sidecar = read_sidecar(layout.chunk_sidecar_path(config, task)) or {}
    output_exists = layout.chunk_output_path(config, task).exists()
    out_path = logs.get("out") if logs else None
    err_path = logs.get("err") if logs else None
    has_log = bool(out_path or err_path)
    err_text = read_tail(err_path)

    status = sidecar.get("status")
    start = _parse_utc(sidecar.get("start_utc"))
    kill = _slurm_kill(err_text)
    node = sidecar.get("node") or (kill["node"] if kill else None)

    diagnosis: dict[str, Any] = {
        "task_index": int(task["task_index"]),
        "job_label": str(task["job_label"]),
        "chunk_id": int(task["chunk_id"]),
        "seed": int(task["seed"]),
        "node": node,
        "slurm_job_id": (
            f"{logs['array_job_id']}_{int(task['task_index'])}" if logs else None
        ),
        "array_job_id": int(logs["array_job_id"]) if logs else None,
        "out_log": str(out_path) if out_path else None,
        "err_log": str(err_path) if err_path else None,
        "start_utc": sidecar.get("start_utc"),
        "elapsed_sec": sidecar.get("duration_sec"),
        "last_activity_utc": None,
        "reason": "",
    }

    def done(mode: str, reason: str = "") -> dict[str, Any]:
        diagnosis["mode"] = mode
        diagnosis["reason"] = reason
        return diagnosis

    if status == "finished":
        if sidecar.get("valid") and output_exists:
            return done("ok")
        return done("invalid", "output missing" if sidecar.get("valid") else "output failed validation")
    if not sidecar and output_exists:
        # Written before sidecars existed: the file itself is the only evidence.
        from .validate_output import validate_file

        if validate_file(layout.chunk_output_path(config, task), expected_events=None)["valid"]:
            return done("ok", "no sidecar; output file valid")
        return done("invalid", "no sidecar; output failed validation")

    # Slurm's own messages first: a task Slurm killed never got to update its
    # sidecar, which therefore still says "running".
    if _OOM.search(err_text):
        return done("oom", "Slurm detected an oom_kill event")
    if kill:
        due = kill["due"]
        if due == "TIME LIMIT":
            return done("timeout", f"Slurm: cancelled at {kill['at']} due to time limit")
        if due == "NODE FAILURE":
            return done("node_failure", f"Slurm: cancelled at {kill['at']} due to node failure")
        suffix = f" due to {due.lower()}" if due else ""
        return done("cancelled", f"Slurm: cancelled at {kill['at']}{suffix}")

    out_text = read_tail(out_path)
    if status == "failed":
        return done("crash", _failure_reason(err_text, out_text))

    if not sidecar and not has_log:
        return done("no_output")

    activity = [t for t in (_mtime(out_path), _mtime(err_path)) if t]
    raw_dir = layout.raw_output_path(config, task).parent
    activity += [mtime for mtime, _, _ in newest_files(raw_dir, 1)]
    last_activity = max(activity) if activity else start
    if last_activity:
        diagnosis["last_activity_utc"] = last_activity.isoformat()
    if start:
        diagnosis["elapsed_sec"] = max(0.0, (now - start).total_seconds())

    if not sidecar:
        # A log but no sidecar: the task died before reaching Python (e.g. the
        # container image was missing), unless it only just started.
        if last_activity and now - last_activity < hung_after:
            return done("running", "started, no sidecar yet")
        return done("crash", _failure_reason(err_text, out_text) or "died before writing a sidecar")

    if start and time_limit is not None and now - start > time_limit + _TIME_LIMIT_GRACE:
        return done("timeout", "still marked running past the Slurm time limit (no Slurm message)")
    if last_activity and now - last_activity > hung_after:
        silent_h = (now - last_activity).total_seconds() / 3600
        return done("hung", f"no log or output activity for {silent_h:.1f} h")
    return done("running")


def load_manifest_tasks(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tasks of the saved manifest, which carries any reseeded seeds.

    Falls back to building the manifest from the configuration when none has
    been written yet; paths depend only on the job label and chunk id, so both
    give the same files.
    """
    from .slurm import build_task_manifest, default_manifest_path

    path = default_manifest_path(dict(config))
    if path.is_file():
        return list(json.loads(path.read_text(encoding="utf-8"))["tasks"])
    return list(build_task_manifest(dict(config))["tasks"])


def diagnose_run(
    config: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    hung_after_hours: float = DEFAULT_HUNG_AFTER_HOURS,
) -> list[dict[str, Any]]:
    """Diagnose every task of a run, adding each job's median chunk duration."""
    now = now or datetime.now(timezone.utc)
    tasks = list(tasks) if tasks is not None else load_manifest_tasks(config)
    logs = find_task_logs(config)
    time_limit = parse_slurm_time((config.get("slurm") or {}).get("time"))
    hung_after = timedelta(hours=hung_after_hours)

    diagnoses = [
        diagnose_task(config, task, logs.get(int(task["task_index"])), now, hung_after, time_limit)
        for task in tasks
    ]

    durations: dict[str, list[float]] = defaultdict(list)
    for task, diagnosis in zip(tasks, diagnoses):
        if diagnosis["mode"] == "ok":
            sidecar = read_sidecar(layout.chunk_sidecar_path(config, task)) or {}
            if sidecar.get("duration_sec") is not None:
                durations[diagnosis["job_label"]].append(float(sidecar["duration_sec"]))
    for diagnosis in diagnoses:
        values = durations.get(diagnosis["job_label"])
        diagnosis["job_median_sec"] = statistics.median(values) if values else None
    return diagnoses


def mode_counts(diagnoses: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Number of tasks per mode, in display order, omitting empty modes."""
    counts = Counter(d["mode"] for d in diagnoses)
    return {mode: counts[mode] for mode in ALL_MODES if counts[mode]}


def node_summary(diagnoses: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-node failure counts, for nodes with at least one failure.

    Only tasks whose node is known count; ``no_output`` tasks never are.
    """
    total: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    modes: dict[str, Counter[str]] = defaultdict(Counter)
    for diagnosis in diagnoses:
        node = diagnosis.get("node")
        if not node:
            continue
        total[node] += 1
        if diagnosis["mode"] not in HEALTHY_MODES:
            failed[node] += 1
            modes[node][diagnosis["mode"]] += 1
    return [
        {
            "node": node,
            "failed": failed[node],
            "total_tasks": total[node],
            "modes": dict(modes[node]),
        }
        for node in sorted(failed, key=lambda n: (-failed[n], n))
    ]


def is_slow(diagnosis: Mapping[str, Any], factor: float = 2.0) -> bool:
    """Whether a running task has taken over ``factor`` times its job's median."""
    elapsed, median = diagnosis.get("elapsed_sec"), diagnosis.get("job_median_sec")
    return elapsed is not None and median is not None and elapsed > factor * median


def _bracket_command(prefix: str, diagnoses: Iterable[Mapping[str, Any]]) -> list[str]:
    from .slurm import format_slurm_array_spec

    by_array: dict[int, list[int]] = defaultdict(list)
    for diagnosis in diagnoses:
        if diagnosis.get("array_job_id") is not None:
            by_array[int(diagnosis["array_job_id"])].append(int(diagnosis["task_index"]))
    return [
        f"{prefix} {array_id}_[{format_slurm_array_spec(indices)}]"
        for array_id, indices in sorted(by_array.items())
    ]


def scancel_commands(diagnoses: Iterable[Mapping[str, Any]]) -> list[str]:
    """``scancel`` lines for the given tasks, one per Slurm array job."""
    return _bracket_command("scancel", diagnoses)


def sacct_commands(
    diagnoses: Iterable[Mapping[str, Any]], array_ids: Sequence[int]
) -> list[str]:
    """``sacct`` lines asking Slurm what became of tasks that left no trace.

    Such tasks have no log, so their array job ID is unknown; one line is given
    per array job of the run.
    """
    indices = sorted(int(d["task_index"]) for d in diagnoses)
    fmt = "--format=JobID%20,State,NodeList,ExitCode"
    return [
        f"sacct -j {','.join(f'{array_id}_{i}' for i in indices)} {fmt}"
        for array_id in array_ids
    ]


def _hours(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 3600:.1f}h"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = [tuple(header), *[tuple(row) for row in rows]]
    widths = [max(len(line[col]) for line in lines) for col in range(len(header))]
    return "\n".join(
        "  ".join(value.ljust(widths[col]) for col, value in enumerate(line)).rstrip()
        for line in lines
    )


def format_failure_table(diagnoses: Sequence[Mapping[str, Any]], max_reason: int = 160) -> str:
    """One row per task: index, job, chunk, node, elapsed vs. job median, reason."""
    rows = []
    for d in sorted(diagnoses, key=lambda d: d["task_index"]):
        reason = str(d.get("reason") or "")
        if len(reason) > max_reason:
            reason = reason[: max_reason - 3] + "..."
        rows.append(
            (
                str(d["task_index"]),
                str(d["job_label"]),
                str(d["chunk_id"]),
                str(d.get("node") or "-"),
                _hours(d.get("elapsed_sec")),
                _hours(d.get("job_median_sec")),
                reason,
            )
        )
    return _table(("TASK", "JOB", "CHUNK", "NODE", "ELAPSED", "JOB_MEDIAN", "REASON"), rows)


def format_node_table(nodes: Sequence[Mapping[str, Any]]) -> str:
    rows = [
        (
            str(n["node"]),
            str(n["failed"]),
            str(n["total_tasks"]),
            ", ".join(f"{mode} {count}" for mode, count in n["modes"].items()),
        )
        for n in nodes
    ]
    return _table(("NODE", "FAILED", "TOTAL_TASKS", "MODES"), rows)


def format_diagnosis_report(
    diagnoses: Sequence[Mapping[str, Any]], array_ids: Sequence[int]
) -> str:
    """The "Failed tasks by mode" and "Nodes" sections of ``check-status``."""
    sections: list[str] = []
    counts = mode_counts(diagnoses)
    sections.append(
        "Tasks by mode: " + (", ".join(f"{mode} {n}" for mode, n in counts.items()) or "(none)")
    )

    running = [d for d in diagnoses if d["mode"] == "running"]
    slow = [d for d in running if is_slow(d)]
    if slow:
        sections.append(
            f"\nrunning, over 2x their job's median duration ({len(slow)} of {len(running)}):\n"
            + format_failure_table(slow)
        )

    for mode in FAILURE_MODES:
        selected = [d for d in diagnoses if d["mode"] == mode]
        if not selected:
            continue
        block = [f"\n{mode} ({len(selected)}):", format_failure_table(selected)]
        if mode == "hung":
            block.append("Cancel them from the submission node, then retry with --modes cancelled:")
            block += [f"  {line}" for line in scancel_commands(selected)]
        if mode == "no_output":
            block.append(NO_OUTPUT_NOTE)
            commands = sacct_commands(selected, array_ids)
            if commands:
                block.append("Ask Slurm what happened to them (from the submission node):")
                block += [f"  {line}" for line in commands]
            else:
                block.append("No Slurm logs found for this run, so its array job ID is unknown.")
        sections.append("\n".join(block))

    nodes = node_summary(diagnoses)
    if nodes:
        sections.append("\nNodes with failures:\n" + format_node_table(nodes))
    return "\n".join(sections)


def format_task_detail(
    config: Mapping[str, Any],
    task: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    lines: int = 20,
    files: int = 10,
) -> str:
    """Everything known about one task: diagnosis, sidecar, log tails, raw files."""
    sidecar = read_sidecar(layout.chunk_sidecar_path(config, task))
    out = [
        f"Task {diagnosis['task_index']}: {diagnosis['job_label']} chunk {diagnosis['chunk_id']}",
        f"  mode:          {diagnosis['mode']}",
        f"  reason:        {diagnosis.get('reason') or '-'}",
        f"  node:          {diagnosis.get('node') or '-'}",
        f"  seed:          {diagnosis['seed']}",
        f"  slurm job:     {diagnosis.get('slurm_job_id') or '-'}",
        f"  started:       {diagnosis.get('start_utc') or '-'}",
        f"  last activity: {diagnosis.get('last_activity_utc') or '-'}",
        f"  elapsed:       {_hours(diagnosis.get('elapsed_sec'))}"
        f" (job median {_hours(diagnosis.get('job_median_sec'))})",
        f"  out log:       {diagnosis.get('out_log') or '-'}",
        f"  err log:       {diagnosis.get('err_log') or '-'}",
        f"  sidecar:       {json.dumps(sidecar, sort_keys=True) if sidecar else '-'}",
    ]
    for label, key in (("stdout", "out_log"), ("stderr", "err_log")):
        path = Path(diagnosis[key]) if diagnosis.get(key) else None
        tail = tail_lines(path, lines)
        out.append(f"\n  last {len(tail)} lines of {label}:")
        out += [f"    {line}" for line in tail] or ["    (none)"]
    raw_dir = layout.raw_output_path(config, task).parent
    newest = newest_files(raw_dir, files)
    out.append(f"\n  newest files in {raw_dir}:")
    out += [
        f"    {mtime.isoformat(timespec='seconds')}  {size:>12}  {name}"
        for mtime, size, name in newest
    ] or ["    (none)"]
    return "\n".join(out)

