from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.common_output import read_events
from neutrino_factory.config import load_config
from neutrino_factory.local import run_local


class LocalPipelineTests(unittest.TestCase):
    def test_local_stub_pipeline_produces_merged_hdf5(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_SOFTWARE_ROOT": f"{tmpdir}/software",
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_SCRATCH_ROOT": f"{tmpdir}/scratch",
                "NF_EXECUTION_MODE": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                result = run_local(config)

                # One merged file per (generator, code_version, config_version).
                instances = config["enabled_generator_instances"]
                merged_outputs = result["merged_outputs"]
                self.assertEqual(len(merged_outputs), len(instances))

                events_per_instance = int(config["run"]["events"])
                seen_version_ids = set()
                for merged_path in merged_outputs:
                    self.assertTrue(Path(merged_path).exists())
                    metadata, events = read_events(merged_path)
                    # Each merged file holds exactly one version's events.
                    self.assertEqual(len(events), events_per_instance)
                    self.assertEqual(metadata["executor"], "local")
                    self.assertIn("code_version", metadata)
                    self.assertIn("config_version", metadata)
                    seen_version_ids.add(metadata["generator_version_id"])
                    # Version info is carried in metadata, not on individual events.
                    self.assertFalse(any("generator_version_id" in event for event in events))

                # Every enabled instance produced its own merged file.
                self.assertEqual(
                    seen_version_ids,
                    {entry["version_id"] for entry in instances},
                )

                # Each per-chunk output carries both version axes in its metadata.
                chunk_metadata, _ = read_events(result["chunk_outputs"][0])
                self.assertIn("code_version", chunk_metadata)
                self.assertIn("config_version", chunk_metadata)
                self.assertIn("generator_version_id", chunk_metadata)


if __name__ == "__main__":
    unittest.main()
