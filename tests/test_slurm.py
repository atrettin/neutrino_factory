from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.config import load_config
from neutrino_factory.slurm import build_task_manifest, render_sbatch_script


class SlurmPlanningTests(unittest.TestCase):
    def test_manifest_and_sbatch_rendering(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_SCRATCH_ROOT": f"{tmpdir}/scratch",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                manifest = build_task_manifest(config)
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

            instance_count = len(config["enabled_generator_instances"])
            expected_tasks = instance_count * int(config["splitting"]["chunks"])
            self.assertEqual(manifest["task_count"], expected_tasks)
            self.assertIn(f"#SBATCH --array=0-{expected_tasks - 1}", script)
        self.assertIn("jobs/run_task.sh", script)


if __name__ == "__main__":
    unittest.main()
