from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.config import load_env_file
from neutrino_factory.setup_wizard import run_setup


def _wizard_args(**overrides) -> argparse.Namespace:
    values = {"pathway": None, "defaults": True, "force": True, "no_build": True}
    values.update(overrides)
    return argparse.Namespace(**values)


class LoadEnvFileTests(unittest.TestCase):
    def test_parses_and_defers_to_real_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            (root / ".env").write_text(
                "# comment\n"
                "NF_TEST_UNSET=from_env_file\n"
                "NF_TEST_PRESET=from_env_file  # inline comment\n"
                "not a valid line\n",
                encoding="utf-8",
            )
            nested = root / "a" / "b"
            nested.mkdir(parents=True)

            env = {"NF_TEST_PRESET": "from_real_env"}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("NF_TEST_UNSET", None)
                loaded = load_env_file(start=nested)
                assert loaded is not None
                self.assertEqual(loaded.resolve(), (root / ".env").resolve())
                self.assertEqual(os.environ["NF_TEST_UNSET"], "from_env_file")
                self.assertEqual(os.environ["NF_TEST_PRESET"], "from_real_env")
            os.environ.pop("NF_TEST_UNSET", None)

    def test_returns_none_without_repo_or_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(load_env_file(start=tmpdir))
            (Path(tmpdir) / "pyproject.toml").write_text("", encoding="utf-8")
            self.assertIsNone(load_env_file(start=tmpdir))


class SetupWizardTests(unittest.TestCase):
    def _run_in_tmp_repo(self, tmpdir: str, **overrides) -> Path:
        root = Path(tmpdir)
        (root / "pyproject.toml").write_text("", encoding="utf-8")
        # patch.dict restores os.environ afterwards (the wizard exports the
        # values it writes, which must not leak into other tests).
        with patch.dict(os.environ, {}, clear=False):
            with patch("neutrino_factory.setup_wizard.find_repo_root", return_value=root):
                self.assertEqual(run_setup(_wizard_args(**overrides)), 0)
        return root / ".env"

    def test_defaults_apptainer_writes_env_with_ptmp_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("neutrino_factory.setup_wizard.getpass.getuser", return_value="testuser"):
                env_path = self._run_in_tmp_repo(tmpdir, pathway="apptainer")
            content = env_path.read_text(encoding="utf-8")

        self.assertIn("NF_CONTAINER_RUNTIME=apptainer", content)
        self.assertIn("NF_IMAGE_ROOT=/ptmp/mpp/testuser/neutrino_factory/images", content)
        self.assertIn("NF_SCRATCH_ROOT=/scratch/testuser/neutrino_factory", content)
        self.assertIn("APPTAINER_CACHEDIR=/ptmp/mpp/testuser/apptainer_cache", content)

    def test_defaults_docker_writes_repo_local_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = self._run_in_tmp_repo(tmpdir, pathway="docker")
            content = env_path.read_text(encoding="utf-8")
            root = Path(tmpdir)

            self.assertIn("NF_CONTAINER_RUNTIME=docker", content)
            self.assertIn(f"NF_SOFTWARE_ROOT={root / 'software'}", content)
            self.assertIn(f"NF_IMAGE_ROOT={root / 'software' / 'images'}", content)
            self.assertNotIn("APPTAINER_CACHEDIR", content)
            # Directories are created.
            self.assertTrue((root / "software" / "images").is_dir())
            self.assertTrue((root / "work").is_dir())

    def test_rerun_prefills_from_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            custom = root / "custom_output"
            (root / ".env").write_text(
                f"NF_CONTAINER_RUNTIME=docker\nNF_OUTPUT_ROOT={custom}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                with patch("neutrino_factory.setup_wizard.find_repo_root", return_value=root):
                    self.assertEqual(run_setup(_wizard_args()), 0)
            content = (root / ".env").read_text(encoding="utf-8")

        self.assertIn("NF_CONTAINER_RUNTIME=docker", content)
        self.assertIn(f"NF_OUTPUT_ROOT={custom}", content)


if __name__ == "__main__":
    unittest.main()
