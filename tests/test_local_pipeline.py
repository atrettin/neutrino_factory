from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.common_output import read_events
from neutrino_factory.config import load_config
from neutrino_factory.cli import build_parser
from neutrino_factory.local import run_local
from neutrino_factory.status import read_sidecar

from .helpers import job, run_config


class LocalPipelineTests(unittest.TestCase):
    def test_local_stub_pipeline_produces_merged_hdf5(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_SOFTWARE_ROOT": f"{tmpdir}/software",
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_EXECUTION_MODE": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                result = run_local(config)

                # One merged file per job.
                jobs = config["jobs"]
                merged_outputs = result["merged_outputs"]
                self.assertEqual(len(merged_outputs), len(jobs))

                seen_labels = set()
                for merged_path, job in zip(merged_outputs, jobs):
                    self.assertTrue(Path(merged_path).exists())
                    metadata, events = read_events(merged_path)
                    # Each merged file holds exactly one job's events.
                    self.assertEqual(len(events), int(job["events"]))
                    self.assertEqual(metadata["executor"], "local")
                    self.assertIn("code_version", metadata)
                    self.assertIn("config_version", metadata)
                    seen_labels.add(metadata["job_label"])
                    # Version info is carried in metadata, not on individual events.
                    self.assertFalse(any("generator_version_id" in event for event in events))

                # Every job produced its own merged file.
                self.assertEqual(seen_labels, {job["label"] for job in jobs})

                # Each per-chunk output carries both version axes in its metadata.
                chunk_metadata, _ = read_events(result["chunk_outputs"][0])
                self.assertIn("code_version", chunk_metadata)
                self.assertIn("config_version", chunk_metadata)
                self.assertIn("generator_version_id", chunk_metadata)

                # Every chunk wrote a finished, valid sidecar beside its HDF5.
                chunks_root = Path(f"{tmpdir}/output") / "chunks"
                sidecars = sorted(chunks_root.rglob("*.sidecar.json"))
                self.assertEqual(len(sidecars), len(result["chunk_outputs"]))
                for sidecar in sidecars:
                    payload = read_sidecar(sidecar)
                    assert payload is not None
                    self.assertEqual(payload["status"], "finished")
                    self.assertTrue(payload["valid"])
                    self.assertIn("start_utc", payload)
                    self.assertIn("stop_utc", payload)
                    self.assertIn("job_label", payload)
                    self.assertIn("execution_mode", payload)
                    self.assertEqual(payload["execution_mode"], "local")

    def test_jobs_differing_only_in_flavour_stay_separate(self) -> None:
        # Two jobs can share a generator and version and still describe
        # different physics. Grouping merged output by (generator, version)
        # would silently mix their events into one file.
        jobs = [
            job(flux={"type": "power_law", "particle": particle,
                      "emin_gev": 0.5, "emax_gev": 10.0, "gamma": -2.0},
                target={"nucleus": nucleus},
                events=4, chunks=2)
            for particle, nucleus in (("numu", "C12"), ("numubar", "Ar40"))
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_SOFTWARE_ROOT": f"{tmpdir}/software",
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
            }
            with patch.dict(os.environ, env, clear=False):
                config = run_config(jobs)
                result = run_local(config)

            self.assertEqual(len(result["merged_outputs"]), 2)
            seen = {}
            for merged_path in result["merged_outputs"]:
                metadata, events = read_events(merged_path)
                self.assertEqual(len(events), 4)
                # Every event carries the initial state its own job asked for.
                self.assertEqual({event["probe"] for event in events},
                                 {metadata["probe"]})
                self.assertEqual({event["target"] for event in events},
                                 {metadata["target_nucleus"]})
                seen[metadata["probe"]] = metadata["target_nucleus"]

            self.assertEqual(seen, {"numu": "C12", "numubar": "Ar40"})

    def test_check_status_reports_per_job_stats(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_SOFTWARE_ROOT": f"{tmpdir}/software",
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_EXECUTION_MODE": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                run_local(config)

                import io
                from contextlib import redirect_stdout
                args = build_parser().parse_args(["check-status", "--config", str(config_path)])
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(args.func(args), 0)
                text = buffer.getvalue()
                self.assertIn("Per-job status", text)
                self.assertIn("STARTED", text)
                self.assertIn("FINISHED", text)
                self.assertIn("VALID", text)
                for job in config["jobs"]:
                    self.assertIn(job["label"], text)


if __name__ == "__main__":
    unittest.main()
