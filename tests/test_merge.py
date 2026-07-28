from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py

from neutrino_factory.common_output import (
    MergeError,
    merge_hdf5_files,
    read_events,
    write_common_hdf5,
)
from neutrino_factory.kinematics import FIELD_DEFAULTS, KINEMATIC_FIELDS


def _write_chunk(path: Path, generator: str, code_version: str, config_version: str, n: int) -> str:
    metadata = {
        "generator": generator,
        "code_version": code_version,
        "config_version": config_version,
        "generator_version_id": f"{code_version}+{config_version}",
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 1,
    }
    events = [
        {
            "event_id": i,
            "seed": 1,
            "energy_gev": 1.0 + i,
            "weight": 1.0,
            "xsec_weight": 1.0,
            "interaction": "qel",
            "probe": "numu",
            "target": "Ar40",
            "generator": generator,
        }
        for i in range(n)
    ]
    return write_common_hdf5(path, metadata, events)


class MergeValidationTests(unittest.TestCase):
    def test_merge_consistent_files_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            a = _write_chunk(d / "a.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 2)
            b = _write_chunk(d / "b.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 3)
            out = d / "merged.h5"

            merge_hdf5_files([a, b], out)

            metadata, events = read_events(out)
            self.assertEqual(len(events), 5)
            self.assertEqual(metadata["code_version"], "R-3_06_00")
            self.assertEqual(metadata["config_version"], "G18_10a_02_11a")
            self.assertEqual(metadata["generator"], "genie")

    def test_merge_different_generators_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            a = _write_chunk(d / "a.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 2)
            b = _write_chunk(d / "b.h5", "nuwro", "nuwro_25.11", "default", 2)
            with self.assertRaises(MergeError):
                merge_hdf5_files([a, b], d / "merged.h5")

    def test_merge_different_config_versions_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            a = _write_chunk(d / "a.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 2)
            b = _write_chunk(d / "b.h5", "genie", "R-3_06_00", "AR23_20i_00_000", 2)
            with self.assertRaises(MergeError):
                merge_hdf5_files([a, b], d / "merged.h5")

    def test_merge_different_code_versions_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            a = _write_chunk(d / "a.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 2)
            b = _write_chunk(d / "b.h5", "genie", "R-3_04_00", "G18_10a_02_11a", 2)
            with self.assertRaises(MergeError):
                merge_hdf5_files([a, b], d / "merged.h5")


class LegacyFileCompatibilityTests(unittest.TestCase):
    """Files written before the kinematic columns existed must stay usable."""

    def _write_legacy_chunk(self, path: Path, n: int) -> str:
        events = [
            {
                "event_id": i,
                "seed": 1,
                "energy_gev": 1.0 + i,
                "weight": 1.0,
                "xsec_weight": 1.0,
                "interaction": "qel",
                "probe": "numu",
                "target": "Ar40",
                "generator": "genie",
            }
            for i in range(n)
        ]
        write_common_hdf5(path, {"generator": "genie"}, events)
        # Strip the kinematic datasets to emulate a file from the older schema.
        with h5py.File(path, "a") as handle:
            for field in KINEMATIC_FIELDS:
                del handle["events"][field]
        return str(path)

    def test_read_events_fills_missing_kinematics_with_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_legacy_chunk(Path(tmpdir) / "legacy.h5", 3)

            _, events = read_events(path)

            self.assertEqual(len(events), 3)
            for event in events:
                self.assertAlmostEqual(event["energy_gev"], 1.0 + event["event_id"])
                for field in KINEMATIC_FIELDS:
                    self.assertEqual(event[field], FIELD_DEFAULTS[field], msg=field)

    def test_merging_legacy_and_current_files_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            legacy = self._write_legacy_chunk(d / "legacy.h5", 2)
            current = _write_chunk(d / "current.h5", "genie", "unknown", "unknown", 2)

            merged = merge_hdf5_files([legacy, current], d / "merged.h5")

            _, events = read_events(merged)
            self.assertEqual(len(events), 4)
            for event in events:
                for field in KINEMATIC_FIELDS:
                    self.assertEqual(event[field], FIELD_DEFAULTS[field], msg=field)

    def test_a_missing_required_column_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.h5"
            self._write_legacy_chunk(path, 2)
            with h5py.File(path, "a") as handle:
                del handle["events"]["energy_gev"]

            with self.assertRaises(KeyError):
                read_events(path)


if __name__ == "__main__":
    unittest.main()
