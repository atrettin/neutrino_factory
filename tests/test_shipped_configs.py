from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from neutrino_factory.config import resolve_config

CONFIG_ROOT = Path(__file__).resolve().parents[1] / "configs"


class ShippedConfigTests(unittest.TestCase):
    """Every configuration in the repository must still load.

    Cheap insurance that a schema change is migrated everywhere, including the
    annotated template under configs/schema, which is documentation people copy
    from and would otherwise rot silently.
    """

    def test_every_shipped_config_resolves_to_at_least_one_job(self) -> None:
        paths = sorted(CONFIG_ROOT.rglob("*.yaml"))
        self.assertTrue(paths, "no shipped configurations found")

        for path in paths:
            with self.subTest(config=str(path.relative_to(CONFIG_ROOT))):
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                # Availability of cross-section splines and container images is
                # a property of the machine, not of the configuration, so the
                # non-stub examples are checked in stub mode here.
                payload.setdefault("run", {})["stub_mode"] = True
                config = resolve_config(payload, source_path=path)

                self.assertGreaterEqual(len(config["jobs"]), 1)
                for job in config["jobs"]:
                    self.assertIn("label", job)
                    self.assertIn("pdg", job["target"])
                    self.assertGreaterEqual(int(job["events"]), 1)


if __name__ == "__main__":
    unittest.main()
