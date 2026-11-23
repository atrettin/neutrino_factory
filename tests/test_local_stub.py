from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from neutrino_factory.config import resolve_config
from neutrino_factory.generators.nuwro import NuWroAdapter
from neutrino_factory.local import run_task
from neutrino_factory.slurm import build_task_manifest

from .helpers import job, run_config


class RealRunWithoutGeneratorTest(unittest.TestCase):
    def test_unavailable_generator_fails_instead_of_running_the_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"NF_OUTPUT_ROOT": f"{tmp}/output", "NF_WORK_ROOT": f"{tmp}/work"}
        ):
            config = run_config(
                [job(generator="nuwro", code_version="nuwro_25.11", config_version="sf",
                     target={"nucleus": "C12"})],
                run={"stub_mode": False},
            )
            task = build_task_manifest(config)["tasks"][0]
            with patch.object(NuWroAdapter, "is_available", return_value=False), \
                 patch.object(NuWroAdapter, "run_stub") as stub:
                with self.assertRaisesRegex(RuntimeError, "not available"):
                    run_task(config, task)
            stub.assert_not_called()

    def test_stub_mode_defaults_to_false(self) -> None:
        self.assertFalse(resolve_config({"jobs": [job()]})["run"]["stub_mode"])


if __name__ == "__main__":
    unittest.main()
