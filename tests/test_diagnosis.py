from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from neutrino_factory import layout
from neutrino_factory.cli import build_parser
from neutrino_factory.config import load_config
from neutrino_factory.diagnosis import (
    collapse_carriage_returns,
    diagnose_run,
    find_task_logs,
    node_summary,
    parse_slurm_time,
    sacct_commands,
    scancel_commands,
)
from neutrino_factory.jobs import job_seed
from neutrino_factory.slurm import build_task_manifest, reseed_tasks, write_manifest

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
RUN = "diag_run"


def _config(root: Path) -> dict:
    return {
        "run": {"name": RUN, "seed": 7, "executor": "slurm"},
        "storage": {"output_root": str(root / "output"), "work_root": str(root / "work")},
        "slurm": {"time": "10:00:00"},
    }


def _task(index: int, label: str = "jobA") -> dict:
    return {
        "run_name": RUN,
        "job_label": label,
        "task_index": index,
        "chunk_id": index,
        "generator_name": "genie",
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "seed": 1000 + index,
        "event_count": 10,
    }


def _ago(hours: float) -> float:
    return (NOW - timedelta(hours=hours)).timestamp()


class DiagnoseRunTests(unittest.TestCase):
    """One synthetic task per failure mode, all read back in a single diagnose_run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = _config(self.root)
        self.logs = self.root / "work" / "logs"
        self.logs.mkdir(parents=True)
        self.tasks: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, sidecar: dict | None = None, out: str | None = None, err: str | None = None,
             output: bool = False, log_age_h: float = 0.1, array: int = 500) -> int:
        index = len(self.tasks)
        task = _task(index)
        self.tasks.append(task)
        if sidecar is not None:
            path = layout.chunk_sidecar_path(self.config, task)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sidecar), encoding="utf-8")
        if output:
            path = layout.chunk_output_path(self.config, task)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stand-in", encoding="utf-8")
        for ext, text in (("out", out), ("err", err)):
            if text is not None:
                log = self.logs / f"nf_{RUN}_{array}_{index}.{ext}"
                log.write_text(text, encoding="utf-8")
                os.utime(log, (_ago(log_age_h), _ago(log_age_h)))
        return index

    def _running(self, hours: float, node: str = "et01") -> dict:
        start = (NOW - timedelta(hours=hours)).isoformat()
        return {"status": "running", "start_utc": start, "node": node}

    def test_every_mode(self) -> None:
        kill = "slurmstepd: error: *** JOB 500_{i} ON et38 CANCELLED AT 2026-10-28T11:00:00{due} ***\n"
        ok = self._add({"status": "finished", "valid": True, "duration_sec": 600.0, "node": "et01"},
                       output=True)
        invalid = self._add({"status": "finished", "valid": False})
        crash = self._add(
            {"status": "failed", "start_utc": NOW.isoformat(), "node": "et38"},
            out="  10 % of events ready\r  20 % of events ready\ninput_data error: out of bounds.\n",
            err="Traceback (most recent call last):\n  File x\n"
                "uproot.exceptions.KeyInFileError: not found: 'treeout'\n",
        )
        oom = self._add(self._running(1), err="slurmstepd: error: Detected 1 oom_kill event in StepId=1.batch.\n")
        timeout = self._add(self._running(9), err=kill.format(i=4, due=" DUE TO TIME LIMIT"))
        cancelled = self._add(self._running(9), err=kill.format(i=5, due=""))
        node_failure = self._add(self._running(2), err=kill.format(i=6, due=" DUE TO NODE FAILURE"))
        hung = self._add(self._running(6), out="Run real events\n", log_age_h=5.5)
        running = self._add(self._running(6), out="Run real events\n", log_age_h=0.2)
        overdue = self._add(self._running(11), out="x\n", log_age_h=0.2)
        no_output = self._add()

        diagnoses = diagnose_run(self.config, self.tasks, now=NOW, hung_after_hours=4)
        modes = {d["task_index"]: d["mode"] for d in diagnoses}

        self.assertEqual(
            modes,
            {ok: "ok", invalid: "invalid", crash: "crash", oom: "oom", timeout: "timeout",
             cancelled: "cancelled", node_failure: "node_failure", hung: "hung",
             running: "running", overdue: "timeout", no_output: "no_output"},
        )
        crash_reason = diagnoses[crash]["reason"]
        self.assertIn("KeyInFileError: not found", crash_reason)
        self.assertNotIn("uproot.exceptions", crash_reason)
        self.assertIn("input_data error: out of bounds", crash_reason)
        # The node comes from Slurm's message when the sidecar has none.
        self.assertEqual(diagnoses[timeout]["node"], "et01")
        self.assertEqual(diagnoses[no_output]["node"], None)
        self.assertEqual(diagnoses[hung]["slurm_job_id"], f"500_{hung}")
        self.assertAlmostEqual(diagnoses[hung]["job_median_sec"], 600.0)

        nodes = {n["node"]: n for n in node_summary(diagnoses)}
        self.assertEqual(nodes["et38"]["failed"], 1)
        self.assertEqual(nodes["et01"]["total_tasks"], 8)

        self.assertEqual(scancel_commands([diagnoses[hung]]), [f"scancel 500_[{hung}]"])
        sacct = sacct_commands([diagnoses[no_output]], [500])
        self.assertEqual(len(sacct), 1)
        self.assertIn(f"500_{no_output}", sacct[0])

    def test_newest_submission_wins(self) -> None:
        index = self._add(self._running(1), err="old crash\n", array=500)
        (self.logs / f"nf_{RUN}_retry_501_{index}.out").write_text("retry\n", encoding="utf-8")
        logs = find_task_logs(self.config)
        self.assertEqual(logs[index]["array_job_id"], 501)


class HelperTests(unittest.TestCase):
    def test_parse_slurm_time(self) -> None:
        self.assertEqual(parse_slurm_time("23:59:00"), timedelta(hours=23, minutes=59))
        self.assertEqual(parse_slurm_time("1-00:00:00"), timedelta(days=1))
        self.assertEqual(parse_slurm_time("30"), timedelta(minutes=30))
        self.assertEqual(parse_slurm_time("2-12"), timedelta(days=2, hours=12))
        self.assertIsNone(parse_slurm_time("soon"))
        self.assertIsNone(parse_slurm_time(None))

    def test_carriage_returns_keep_the_final_state(self) -> None:
        self.assertEqual(collapse_carriage_returns("a 1%\ra 50%\ra 100%\nnext"), ["a 100%", "next"])


class ReseedTests(unittest.TestCase):
    def test_attempt_zero_keeps_existing_seeds(self) -> None:
        self.assertEqual(job_seed(7, "jobA", 3), job_seed(7, "jobA", 3, attempt=0))
        self.assertNotEqual(job_seed(7, "jobA", 3), job_seed(7, "jobA", 3, attempt=1))

    def test_reseed_changes_only_selected_tasks_and_counts_attempts(self) -> None:
        config = {"run": {"seed": 7}}
        manifest = {"tasks": [_task(i) for i in range(4)]}
        untouched = [dict(t) for t in manifest["tasks"]]

        first = reseed_tasks(config, manifest, [1, 3])
        self.assertEqual(sorted(first), [1, 3])
        second = reseed_tasks(config, manifest, [1])

        tasks = manifest["tasks"]
        self.assertEqual(tasks[0], untouched[0])
        self.assertEqual(tasks[2], untouched[2])
        self.assertEqual(tasks[1]["seed_attempt"], 2)
        self.assertEqual(tasks[1]["original_seed"], 1001)
        self.assertEqual(tasks[1]["seed"], job_seed(7, "jobA", 1, attempt=2))
        self.assertEqual(second[1][0], first[1][1])
        self.assertEqual(len({t["seed"] for t in tasks}), len(tasks))


class CliTests(unittest.TestCase):
    """check-status and retry on the example config, with hand-made sidecars."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        args = build_parser().parse_args(argv)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = args.func(args)
        return code, buffer.getvalue()

    def test_retry_skips_running_and_check_merged_is_opt_in(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = str(repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml")
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"NF_OUTPUT_ROOT": f"{tmpdir}/output", "NF_WORK_ROOT": f"{tmpdir}/work"}
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                write_manifest(config)
                tasks = build_task_manifest(config)["tasks"]
                self.assertGreaterEqual(len(tasks), 3)
                now = datetime.now(timezone.utc).isoformat()
                for task, status in zip(tasks, ("running", "failed")):
                    path = layout.chunk_sidecar_path(config, task)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"status": status, "start_utc": now}), encoding="utf-8")

                code, text = self._run(["check-status", "--config", config_path, "--json"])
                self.assertEqual(code, 1)
                payload = json.loads(text)
                self.assertNotIn("merged", payload)
                self.assertEqual(payload["mode_counts"]["running"], 1)
                self.assertEqual(payload["mode_counts"]["crash"], 1)

                _, text = self._run(["check-status", "--config", config_path, "--json", "--check-merged"])
                self.assertIn("merged", json.loads(text))

                _, text = self._run(["retry", "--config", config_path, "--time", "01:00:00", "--executor", "slurm", "--dry-run"])
                retried = json.loads(text[text.rindex("\n{"):])["missing_task_indices"]
                self.assertNotIn(tasks[0]["task_index"], retried)
                self.assertIn(tasks[1]["task_index"], retried)

                _, text = self._run(["retry", "--config", config_path, "--time", "01:00:00", "--executor", "slurm",
                                     "--modes", "crash", "--dry-run"])
                retried = json.loads(text[text.rindex("\n{"):])["missing_task_indices"]
                self.assertEqual(retried, [tasks[1]["task_index"]])

                with self.assertRaisesRegex(RuntimeError, "cannot be retried"):
                    self._run(["retry", "--config", config_path, "--time", "01:00:00", "--executor", "slurm",
                               "--modes", "hung", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
