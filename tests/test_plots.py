from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neutrino_factory.common_output import write_common_hdf5
from neutrino_factory.plots import interaction_counts, make_plots


def _write_output(path: Path) -> str:
    metadata = {
        "generator": "genie",
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "generator_version_id": "R-3_06_00+G18_10a_02_11a",
        "execution_mode": "local",
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 1,
        "flux": {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 10.0,
            "gamma": -2.0,
        },
        "expected_events": 6,
    }
    interactions = ["qel", "qel", "res", "dis", "coh", "mec"]
    events = [
        {
            "event_id": i,
            "seed": 1,
            "energy_gev": 0.5 + i,
            "weight": 1.0 + 0.5 * i,
            "interaction": itype,
            "probe": "numu",
            "target": "Ar40",
            "generator": "genie",
        }
        for i, itype in enumerate(interactions)
    ]
    return write_common_hdf5(path, metadata, events)


class InteractionCountsTests(unittest.TestCase):
    def test_counts_in_canonical_order(self) -> None:
        counts = interaction_counts(["dis", "qel", "qel", "res", "unknown_type"])
        # Known types come first in canonical order; extras are appended.
        self.assertEqual(list(counts.keys()), ["qel", "res", "dis", "unknown_type"])
        self.assertEqual(counts["qel"], 2)
        self.assertEqual(counts["res"], 1)
        self.assertEqual(counts["dis"], 1)
        self.assertEqual(counts["unknown_type"], 1)


class MakePlotsTests(unittest.TestCase):
    def test_writes_four_pngs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            h5_path = Path(tmpdir) / "run_genie_ver.h5"
            _write_output(h5_path)

            written = make_plots(h5_path)

            self.assertEqual(len(written), 4)
            expected = {
                h5_path.parent / "run_genie_ver_interactions.png",
                h5_path.parent / "run_genie_ver_flux.png",
                h5_path.parent / "run_genie_ver_energy.png",
                h5_path.parent / "run_genie_ver_energy_weighted.png",
            }
            self.assertEqual({Path(p) for p in written}, expected)
            for path in written:
                self.assertTrue(Path(path).is_file())
                self.assertGreater(Path(path).stat().st_size, 0)

    def test_output_dir_and_prefix_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            h5_path = Path(tmpdir) / "run.h5"
            _write_output(h5_path)
            out_dir = Path(tmpdir) / "plots"

            written = make_plots(h5_path, output_dir=out_dir, prefix="myrun")

            self.assertEqual(
                {Path(p) for p in written},
                {
                    out_dir / "myrun_interactions.png",
                    out_dir / "myrun_flux.png",
                    out_dir / "myrun_energy.png",
                    out_dir / "myrun_energy_weighted.png",
                },
            )
            for path in written:
                self.assertTrue(Path(path).is_file())


if __name__ == "__main__":
    unittest.main()
