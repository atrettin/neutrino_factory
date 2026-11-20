from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neutrino_factory.generators.nuwro import NuWroAdapter


class NuWroAdapterTests(unittest.TestCase):
    def test_removes_event_files_from_an_earlier_attempt(self) -> None:
        """Parts of an earlier attempt must not sit beside, or be merged with, a retry's."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            for name in (
                "events.root",
                "events.root.0.part",
                "events.root.10.part",
                "unrelated.txt",
            ):
                (work_dir / name).write_bytes(b"left by an earlier attempt")
            translated = {"code_version": "nuwro_25.11", "nuwro_params": {"number_of_events": 10}}
            with self.assertLogs("neutrino_factory.generators.nuwro", "WARNING") as logs:
                NuWroAdapter({}).build_run_command(translated, work_dir)

            self.assertEqual(sorted(p.name for p in work_dir.iterdir() if p.name.startswith("events")), [])
            self.assertTrue((work_dir / "unrelated.txt").is_file())
            self.assertEqual(sum("earlier attempt" in r.getMessage() for r in logs.records), 3)


if __name__ == "__main__":
    unittest.main()
