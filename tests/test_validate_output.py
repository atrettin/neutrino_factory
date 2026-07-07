from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py

from neutrino_factory.common_output import write_common_hdf5
from neutrino_factory.validate_output import (
    check_columns,
    check_event_count,
    summarize,
    validate_file,
)


def _write_valid(path: Path, n: int) -> str:
    metadata = {
        "generator": "genie",
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "generator_version_id": "R-3_06_00+G18_10a_02_11a",
        "execution_mode": "local",
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 1,
        "flux": {"type": "power_law", "particle": "numu", "emin_gev": 0.5, "emax_gev": 10.0, "gamma": -2.0},
        "expected_events": n,
    }
    events = [
        {
            "event_id": i,
            "seed": 1,
            "energy_gev": 1.0 + i,
            "weight": 1.0,
            "interaction": "qel",
            "probe": "numu",
            "target": "Ar40",
            "generator": "genie",
        }
        for i in range(n)
    ]
    return write_common_hdf5(path, metadata, events)


class ValidateOutputTests(unittest.TestCase):
    def test_valid_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "good.h5"
            _write_valid(path, 5)
            result = validate_file(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["errors"], [])
            self.assertTrue(result["exists"])
            self.assertTrue(result["openable"])

    def test_missing_file_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = validate_file(Path(tmpdir) / "nope.h5")
            self.assertFalse(result["exists"])
            self.assertFalse(result["valid"])
            self.assertTrue(result["errors"])

    def test_missing_column_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dropped.h5"
            _write_valid(path, 3)
            with h5py.File(path, "a") as handle:
                del handle["events"]["weight"]
            result = validate_file(path)
            self.assertIn("weight", result["columns"]["missing"])
            self.assertFalse(result["valid"])

    def test_inconsistent_length_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ragged.h5"
            _write_valid(path, 4)
            with h5py.File(path, "a") as handle:
                events = handle["events"]
                del events["weight"]
                events.create_dataset("weight", data=[1.0, 1.0])
            columns = None
            with h5py.File(path, "r") as handle:
                columns = check_columns(handle)
            self.assertFalse(columns["consistent_length"])
            self.assertFalse(validate_file(path)["valid"])

    def test_stored_count_mismatch_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "miscount.h5"
            _write_valid(path, 4)
            with h5py.File(path, "a") as handle:
                handle["run"].attrs["event_count"] = 99
            with h5py.File(path, "r") as handle:
                count = check_event_count(handle)
            self.assertFalse(count["self_consistent"])
            self.assertFalse(validate_file(path)["valid"])

    def test_expected_events_within_and_outside_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "counted.h5"
            _write_valid(path, 100)
            within = validate_file(path, expected_events=98, tolerance=0.05)
            self.assertTrue(within["event_count"]["within_tolerance"])
            self.assertTrue(within["valid"])

            outside = validate_file(path, expected_events=50, tolerance=0.05)
            self.assertFalse(outside["event_count"]["within_tolerance"])
            self.assertFalse(outside["valid"])

    def test_summarize_fractions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            good = validate_file(_write_valid(d / "a.h5", 2))
            missing = validate_file(d / "missing.h5")
            bad = _write_valid(d / "b.h5", 2)
            with h5py.File(bad, "a") as handle:
                del handle["events"]["probe"]
            broken = validate_file(bad)

            summary = summarize([good, missing, broken])
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["existing"], 2)
            self.assertEqual(summary["valid"], 1)
            self.assertEqual(summary["with_errors"], 2)
            self.assertAlmostEqual(summary["fraction_valid"], 1 / 3)
            self.assertAlmostEqual(summary["fraction_existing"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
