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
    format_diagnosis_report,
    node_summary,
    parse_slurm_time,
    read_tail,
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

        # Without --list-failures each mode shows only its count; scancel stays.
        short = format_diagnosis_report(diagnoses, [500])
        self.assertIn("crash (1):", short)
        self.assertNotIn("TASK  JOB", short)
        self.assertNotIn("sacct", short)
        self.assertIn(f"scancel 500_[{hung}]", short)
        self.assertIn("--list-failures", short)
        full = format_diagnosis_report(diagnoses, [500], list_failures=True)
        self.assertIn("TASK  JOB", full)
        self.assertIn(f"500_{no_output}", full)
        self.assertNotIn("Rerun with --list-failures", full)

    def test_real_cluster_messages(self) -> None:
        # Slurm appends advice after the reason for a node failure.
        node_failure = self._add(
            self._running(2),
            err="Results was:\nslurmstepd: error: *** JOB 235778 ON et07 CANCELLED AT "
                "2026-11-03T20:32:21 DUE TO NODE FAILURE, SEE SLURMCTLD LOG FOR DETAILS ***\n",
        )
        # The CLI's own final "Error: ..." line, with GiBUU's banner in stdout.
        crash = self._add(
            {"status": "failed", "start_utc": NOW.isoformat(), "node": "et29"},
            out="**      !!                 hh    !!       of errors and use common sense    **\n",
            err="Error: Cannot read branch 'lepIn_E' from EventOutput.Pert.00000001.root: "
                "not found: 'lepIn_E'\n    Available keys: 'E', 'Px'\n\nin object /RootTuple;1\n",
        )

        diagnoses = diagnose_run(self.config, self.tasks, now=NOW, hung_after_hours=4)

        self.assertEqual(diagnoses[node_failure]["mode"], "node_failure")
        self.assertEqual(diagnoses[crash]["mode"], "crash")
        self.assertEqual(
            diagnoses[crash]["reason"],
            "Error: Cannot read branch 'lepIn_E' from EventOutput.Pert.00000001.root: "
            "not found: 'lepIn_E'",
        )

    def test_failure_reason_lines(self) -> None:
        failed = {"status": "failed", "start_utc": NOW.isoformat(), "node": "et29"}
        died = (
            "subprocess.CalledProcessError: Command '['nf-run', 'neut', '5.7.0', 'neutroot2', "
            "'card']' died with <Signals.SIGFPE: 8>.\n"
        )
        signal = "nf-run neut 5.7.0 neutroot2 ... died with <Signals.SIGFPE: 8>"
        warning = "*** WARNING *** KINEMATICAL ERROR AT NECOHVCT (MODE,E)  16   59.36272"
        filler = "".join(f"event {i}\n" for i in range(100))
        # name: (stdout, stderr, expected reason)
        cases = {
            # A real error after GiBUU's banner is the generator line.
            "banner": (
                "**      !!                 hh    !!       of errors and use common sense    **\n"
                "Error in readInput: namelist not found\n",
                died,
                f"{signal} | out: Error in readInput: namelist not found",
            ),
            # An unrelated "ERROR ..." line in stderr is not a Python exception.
            "stray_stderr_error": (
                None,
                "ERROR PDG : Requested missing particle\nTraceback (most recent call last):\n"
                "ValueError: empty file\nERROR something else afterwards\n",
                "ValueError: empty file",
            ),
            # A routine warning loses to a real error just before it ...
            "error_before_warning": (
                f"FATAL_ERROR in nemkcrsc\n{warning}\n", died, f"{signal} | out: FATAL_ERROR in nemkcrsc"
            ),
            # ... but not to a recovered one far back (NEUT's "Error in neapivct").
            "stale_error": (
                f"Error in neapivct, Mode= -46\n{filler}{warning}\n", died, f"{signal} | out: {warning}"
            ),
            "only_warning": (f"{warning}\n", died, f"{signal} | out: {warning}"),
            # Crash markers without the word "error"; "SIGMAX =" is not a signal.
            "root_break": (
                "Run real events\n*** Break *** illegal instruction\nevent 12\n",
                died,
                f"{signal} | out: *** Break *** illegal instruction",
            ),
            "buffer_overflow": (
                "*** buffer overflow detected ***: terminated\n",
                None,
                "out: *** buffer overflow detected ***: terminated",
            ),
            "sigsegv": ("caught SIGSEGV\nx-sec = 14.9  SIGMAX = 14.0\n", None, "out: caught SIGSEGV"),
            "killed": ("/bin/sh: line 1:  4242 Killed  GiBUU.x\n", None,
                       "out: /bin/sh: line 1:  4242 Killed  GiBUU.x"),
            "cxx_exception": (
                "terminate called after throwing an instance of 'std::bad_alloc'\n", None,
                "out: terminate called after throwing an instance of 'std::bad_alloc'",
            ),
            "assertion": (
                "gevgen: Foo.cxx:12: void f(): Assertion `x > 0' failed.\n", None,
                "out: gevgen: Foo.cxx:12: void f(): Assertion `x > 0' failed.",
            ),
            "core_dumped": ("Floating point exception (core dumped)\n", None,
                            "out: Floating point exception (core dumped)"),
            "no_marker": ("x-sec = 14.9  SIGMAX = 14.0\nsigma = 3\n", None, ""),
        }
        indices = {name: self._add(failed, out=out, err=err) for name, (out, err, _) in cases.items()}

        diagnoses = diagnose_run(self.config, self.tasks, now=NOW, hung_after_hours=4)

        for name, (_, _, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(diagnoses[indices[name]]["reason"], expected)
    def test_slurm_kill_lines(self) -> None:
        # Slurm writes local time (CET here): 11:30 local is 10:30 UTC.
        kill = "slurmstepd: error: *** {kind} 500_{i} ON et38 CANCELLED AT 2026-10-28T11:30:00{due} ***\n"
        preempted = self._add(self._running(2), err=kill.format(kind="JOB", i=0, due=" DUE TO PREEMPTION"))
        step = self._add(self._running(9), err=kill.format(kind="STEP", i=1, due=" DUE TO TIME LIMIT"))
        # A requeued task appends to the same .err: a kill from before the
        # sidecar's start (11:00 UTC) belongs to the earlier attempt and is
        # ignored. Read as UTC, the kill (11:30) would wrongly count.
        requeued = self._add(
            self._running(1),
            out="Run real events\n",
            err=kill.format(kind="JOB", i=2, due=" DUE TO NODE FAILURE, SEE SLURMCTLD LOG FOR DETAILS"),
        )
        # Started 10:20 UTC, killed 10:30 UTC: a kill after the start counts.
        just_started = self._add(
            {"status": "running", "start_utc": "2026-10-28T10:20:00+00:00", "node": "et01"},
            err=kill.format(kind="JOB", i=3, due=""),
        )
        # The kill may predate the start by up to a minute (clock slack).
        in_slack = self._add(
            {"status": "running", "start_utc": "2026-10-28T10:31:00+00:00", "node": "et01"},
            err=kill.format(kind="JOB", i=4, due=""),
        )
        past_slack = self._add(
            {"status": "running", "start_utc": "2026-10-28T10:31:01+00:00", "node": "et01"},
            out="x\n",
            err=kill.format(kind="JOB", i=5, due=""),
        )
        # 02:30 on 2026-10-25 occurs twice in Berlin: 00:30 UTC (CEST) and
        # 01:30 UTC (CET). A start at 01:00 UTC is before the later reading.
        fold = self._add(
            {"status": "running", "start_utc": "2026-10-25T01:00:00+00:00", "node": "et01"},
            err="slurmstepd: error: *** JOB 500_6 ON et38 CANCELLED AT 2026-10-25T02:30:00 ***\n",
        )

        diagnoses = diagnose_run(self.config, self.tasks, now=NOW, hung_after_hours=4)

        self.assertEqual(diagnoses[preempted]["mode"], "cancelled")
        self.assertEqual(
            diagnoses[preempted]["reason"], "Slurm: cancelled at 2026-10-28T11:30:00 due to preemption"
        )
        self.assertEqual(diagnoses[step]["mode"], "timeout")
        self.assertEqual(diagnoses[requeued]["mode"], "running")
        self.assertEqual(diagnoses[just_started]["mode"], "cancelled")
        self.assertEqual(diagnoses[in_slack]["mode"], "cancelled")
        self.assertEqual(diagnoses[past_slack]["mode"], "running")
        self.assertEqual(diagnoses[fold]["mode"], "cancelled")

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

    def test_read_tail_drops_the_cut_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.out"
            path.write_text("*** WARNING *** first\nsecond\nthird\n", encoding="utf-8")
            self.assertEqual(read_tail(path, max_bytes=16), "second\nthird\n")
            self.assertEqual(read_tail(path), "*** WARNING *** first\nsecond\nthird\n")
            # A cut exactly at a line start drops nothing.
            path.write_bytes(b"AAAAAAAAAA\nlast\n")
            self.assertEqual(read_tail(path, max_bytes=5), "last\n")
            # A tail without any line break is kept.
            path.write_bytes(b"x" * 20)
            self.assertEqual(read_tail(path, max_bytes=5), "xxxxx")


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

    def test_retry_refuses_tasks_an_earlier_retry_has_not_started(self) -> None:
        """Two retry scripts must never cover the same task, nor overwrite each other."""
        repo_root = Path(__file__).resolve().parents[1]
        config_path = str(repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml")
        argv = ["retry", "--config", config_path, "--time", "01:00:00", "--executor", "slurm"]
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"NF_OUTPUT_ROOT": f"{tmpdir}/output", "NF_WORK_ROOT": f"{tmpdir}/work"}
            with patch.dict(os.environ, env, clear=False), patch(
                "neutrino_factory.cli.shutil.which", return_value=None
            ):
                config = load_config(config_path)
                write_manifest(config)
                task = build_task_manifest(config)["tasks"][0]
                path = layout.chunk_sidecar_path(config, task)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"status": "failed", "start_utc": "2020-01-01T00:00:00+00:00"}),
                    encoding="utf-8",
                )

                _, text = self._run(argv + ["--dry-run"])
                self.assertIn("dry-run", text)
                self.assertEqual(list((Path(tmpdir) / "work").rglob("*.sbatch")), [])

                _, text = self._run(argv)
                first = json.loads(text[text.rindex("\n{"):])["sbatch_path"]
                self.assertTrue(Path(first).is_file())

                # The task has not started since the script was issued: refuse.
                with self.assertRaisesRegex(RuntimeError, "already covered by"):
                    self._run(argv)

                # --force overrides, and writes a script of its own.
                _, text = self._run(argv + ["--force"])
                second = json.loads(text[text.rindex("\n{"):])["sbatch_path"]
                self.assertNotEqual(first, second)
                self.assertTrue(Path(first).is_file())

                # Once the task has started after the last script, a failure is new.
                path.write_text(
                    json.dumps({"status": "failed", "start_utc": datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8",
                )
                _, text = self._run(argv + ["--modes", "crash"])
                self.assertEqual(
                    json.loads(text[text.rindex("\n{"):])["missing_task_indices"],
                    [task["task_index"]],
                )


if __name__ == "__main__":
    unittest.main()
