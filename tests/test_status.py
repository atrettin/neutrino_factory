from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from neutrino_factory.status import (
    format_status_table,
    finish_sidecar,
    job_status_stats,
    read_sidecar,
    utc_now_iso,
    write_sidecar_start,
)


def _task(chunk_id: int = 0, task_index: int = 0, job_label: str = "jobA") -> dict:
    return {
        "run_name": "run1",
        "job_label": job_label,
        "task_index": task_index,
        "chunk_id": chunk_id,
        "generator_name": "genie",
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "seed": 1 + chunk_id,
        "event_count": 100,
    }


class WriteReadSidecarTests(unittest.TestCase):
    def test_write_start_then_finish(self) -> None:
        config = {"run": {"executor": "local"}, "storage": {"output_root": "/nonexistent"}}
        task = _task()
        with tempfile.TemporaryDirectory() as tmpdir:
            config["storage"]["output_root"] = tmpdir
            path = write_sidecar_start(config, task)

            self.assertTrue(path.is_file())
            running = read_sidecar(path)
            assert running is not None
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["job_label"], "jobA")
            self.assertEqual(running["chunk_id"], 0)
            self.assertEqual(running["execution_mode"], "local")
            self.assertIn("start_utc", running)
            self.assertNotIn("stop_utc", running)

            finish_sidecar(path, valid=True)
            finished = read_sidecar(path)
            assert finished is not None
            self.assertEqual(finished["status"], "finished")
            self.assertTrue(finished["valid"])
            self.assertIn("stop_utc", finished)
            self.assertIn("duration_sec", finished)
            self.assertGreaterEqual(finished["duration_sec"], 0.0)

    def test_finish_records_failure(self) -> None:
        config = {"run": {"executor": "slurm"}, "storage": {"output_root": "/nonexistent"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            config["storage"]["output_root"] = tmpdir
            path = write_sidecar_start(config, _task())
            finish_sidecar(path, valid=False, status="failed")
            payload = read_sidecar(path)
            assert payload is not None
            self.assertEqual(payload["status"], "failed")
            self.assertFalse(payload["valid"])

    def test_read_missing_or_corrupt_returns_none(self) -> None:
        self.assertIsNone(read_sidecar("/nonexistent/sidecar.json"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_sidecar(path))


class JobStatusStatsTests(unittest.TestCase):
    def test_counts_and_timing(self) -> None:
        config = {"run": {"executor": "local"}, "storage": {"output_root": "/nonexistent"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            config["storage"]["output_root"] = tmpdir

            # chunk 0: running only (no stop).
            p0 = write_sidecar_start(config, _task(chunk_id=0, task_index=0))
            # chunk 1: finished and valid.
            p1 = write_sidecar_start(config, _task(chunk_id=1, task_index=1))
            finish_sidecar(p1, valid=True)
            # chunk 2: finished but invalid (failed).
            p2 = write_sidecar_start(config, _task(chunk_id=2, task_index=2))
            finish_sidecar(p2, valid=False)
            # chunk 3: no sidecar at all.
            chunks = [
                {"sidecar_path": str(p0)},
                {"sidecar_path": str(p1)},
                {"sidecar_path": str(p2)},
                {"sidecar_path": str(Path(tmpdir) / "never.json")},
            ]

            stats = job_status_stats(chunks)
            self.assertEqual(stats["total"], 4)
            self.assertEqual(stats["started"], 3)
            self.assertEqual(stats["finished"], 2)
            self.assertEqual(stats["valid"], 1)
            self.assertIsNotNone(stats["first_start_utc"])
            self.assertIsNotNone(stats["last_finish_utc"])
            self.assertIsNotNone(stats["avg_duration_sec"])
            self.assertIsNotNone(stats["max_duration_sec"])
            self.assertGreaterEqual(stats["max_duration_sec"], stats["avg_duration_sec"])
            self.assertGreater(stats["avg_duration_sec"], 0.0)

    def test_no_sidecars_yields_zeros(self) -> None:
        stats = job_status_stats([{"sidecar_path": "/none/x.json"}])
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["started"], 0)
        self.assertEqual(stats["finished"], 0)
        self.assertEqual(stats["valid"], 0)
        self.assertIsNone(stats["first_start_utc"])
        self.assertIsNone(stats["last_finish_utc"])
        self.assertIsNone(stats["avg_duration_sec"])
        self.assertIsNone(stats["max_duration_sec"])

    def test_format_status_table(self) -> None:
        rows = [{"job_label": "jobA", **job_status_stats([{"sidecar_path": "/none/x.json"}])}]
        table = format_status_table(rows)
        self.assertIn("JOB", table)
        self.assertIn("jobA", table)
        self.assertIn("AVG_S", table)

        self.assertEqual(format_status_table([]), "(no jobs)")


if __name__ == "__main__":
    unittest.main()
