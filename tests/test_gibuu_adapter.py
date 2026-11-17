from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.generators.gibuu import GiBUUAdapter

PERT_FILE = "EventOutput.Pert.00000001.root"


def _translated(*names: str) -> dict:
    """A translated config whose passes are ``names`` (current from the prefix)."""
    return {
        "code_version": "release2025",
        "gibuu_passes": [
            {
                "name": name,
                "current": name[:2],
                "channels": "qe" if "_qe" in name else "all",
                "jobcard": f"! {name}",
            }
            for name in names
        ],
    }


def _stage_old_output(work_dir: Path, name: str) -> Path:
    """A pass directory left by an earlier attempt, with its output file."""
    pass_dir = work_dir / name
    pass_dir.mkdir(parents=True)
    (pass_dir / PERT_FILE).write_text("old output", encoding="utf-8")
    return pass_dir


class GiBUUAdapterStalePassTests(unittest.TestCase):
    """A retry reuses the raw directory: passes no longer translated must go,
    passes still translated must stay."""

    def _build(self, translated: dict, work_dir: Path):
        with patch(
            "neutrino_factory.generators.gibuu.shutil.which",
            return_value="/usr/local/bin/GiBUU.x",
        ):
            return GiBUUAdapter({}).build_run_command(translated, work_dir)

    def test_stale_pass_directory_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _stage_old_output(work_dir, "cc_qe03")
            _stage_old_output(work_dir, "cc_qe100")
            (work_dir / "unrelated").mkdir()
            with self.assertLogs("neutrino_factory.generators.gibuu", "WARNING") as logs:
                self._build(_translated("cc", "nc_qe01"), work_dir)

            self.assertEqual(
                sorted(p.name for p in work_dir.iterdir()),
                ["cc", "nc_qe01", "translated_config.json", "unrelated"],
            )
            self.assertIn("cc_qe03", "\n".join(logs.output))
            self.assertIn("cc_qe100", "\n".join(logs.output))

    def test_translated_pass_directory_is_kept(self) -> None:
        # Only directories outside the translated passes are removed; a pass
        # that is still translated keeps its directory and earlier output.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            kept = _stage_old_output(work_dir, "nc_qe01")
            with self.assertNoLogs("neutrino_factory.generators.gibuu", "WARNING"):
                self._build(_translated("cc", "nc_qe01"), work_dir)

            self.assertEqual((kept / PERT_FILE).read_text(encoding="utf-8"), "old output")
            self.assertEqual((kept / "job.job").read_text(encoding="utf-8"), "! nc_qe01")

    def test_stale_pass_symlink_is_unlinked_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work_dir = root / "work"
            work_dir.mkdir()
            target = _stage_old_output(root, "elsewhere")
            (work_dir / "cc_qe01").symlink_to(target, target_is_directory=True)
            with self.assertLogs("neutrino_factory.generators.gibuu", "WARNING") as logs:
                self._build(_translated("cc"), work_dir)

            self.assertFalse((work_dir / "cc_qe01").exists())
            self.assertFalse((work_dir / "cc_qe01").is_symlink())
            self.assertEqual((target / PERT_FILE).read_text(encoding="utf-8"), "old output")
            self.assertIn("symlink", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
