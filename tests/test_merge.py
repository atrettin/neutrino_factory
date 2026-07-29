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
            "is_cc": True,
            "xsec_weight": 1.0,
            "interaction": "qel",
            "probe": "numu",
            "target": "Ar40",
            "generator": generator,
        }
        for i in range(n)
    ]
    return write_common_hdf5(path, metadata, events)


class IsCcRoundTripTests(unittest.TestCase):
    def test_mixed_currents_survive_write_read_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            metadata = {
                "generator": "genie",
                "code_version": "R-3_06_00",
                "config_version": "G18_10a_02_11a",
                "generator_version_id": "R-3_06_00+G18_10a_02_11a",
                "run_name": "test_run",
                "chunk_id": 0,
                "seed": 1,
            }

            def chunk(path: Path, flags: list[bool]) -> str:
                events = [
                    {
                        "event_id": i,
                        "seed": 1,
                        "energy_gev": 1.0 + i,
                        "weight": 1.0,
                        "xsec_weight": 1.0,
                        "is_cc": flag,
                        "interaction": "qel",
                        "probe": "numu",
                        "target": "Ar40",
                        "generator": "genie",
                    }
                    for i, flag in enumerate(flags)
                ]
                return write_common_hdf5(path, metadata, events)

            a = chunk(d / "a.h5", [True, False])
            b = chunk(d / "b.h5", [False, True])
            out = d / "merged.h5"

            merge_hdf5_files([a, b], out)

            _, events = read_events(out)
            flags = [event["is_cc"] for event in events]
            self.assertEqual(flags, [True, False, False, True])
            # Read back as real booleans, not numpy scalars or 0/1 floats.
            self.assertIsInstance(flags[0], bool)

    def test_event_missing_is_cc_is_rejected(self) -> None:
        # No default: writing one would put a fabricated current into output
        # that is indistinguishable from a measured one.
        with tempfile.TemporaryDirectory() as tmpdir:
            event = {
                "event_id": 0,
                "seed": 1,
                "energy_gev": 1.0,
                "weight": 1.0,
                "xsec_weight": 1.0,
                "interaction": "qel",
                "probe": "numu",
                "target": "Ar40",
                "generator": "genie",
            }
            with self.assertRaises(KeyError) as ctx:
                write_common_hdf5(Path(tmpdir) / "bad.h5", {"generator": "genie"}, [event])
            self.assertIn("is_cc", str(ctx.exception))


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
                "is_cc": True,
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


def _write_normalized_chunk(
    path: Path,
    *,
    energies: list[float],
    xsec_weights: list[float],
    norm_count: float | None,
    chunk_id: int = 0,
) -> str:
    """A chunk as a real normalizer writes it, optionally declaring its norm count."""
    metadata = {
        "generator": "neut",
        "code_version": "5.7.0-nuint2024",
        "config_version": "default",
        "generator_version_id": "5.7.0-nuint2024+default",
        "run_name": "test_run",
        "chunk_id": chunk_id,
        "seed": 1,
    }
    if norm_count is not None:
        metadata["xsec_norm_count"] = norm_count
    events = [
        {
            "event_id": i,
            "seed": 1,
            "energy_gev": energy,
            "weight": 1.0,
            "is_cc": True,
            "xsec_weight": xsec_weight,
            "interaction": "qel",
            "probe": "numu",
            "target": "C12",
            "generator": "neut",
        }
        for i, (energy, xsec_weight) in enumerate(zip(energies, xsec_weights))
    ]
    return write_common_hdf5(path, metadata, events)


def _total_xsec_weight(path: Path) -> float:
    _, events = read_events(path)
    return sum(event["xsec_weight"] for event in events)


class MergeCrossSectionNormalizationTests(unittest.TestCase):
    """Merging must average per-chunk cross-section estimates, not sum them.

    Each chunk's `sum(xsec_weight in a bin) / bin_width` already estimates
    sigma(E) on its own, so concatenating N chunks used to report N * sigma.
    """

    def test_equal_chunks_merge_to_the_same_cross_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            # Two independent 100-event chunks, each estimating the same sigma.
            # sum(xsec_weight) per chunk = 100 * 0.02 = 2.0
            for chunk_id in (0, 1):
                _write_normalized_chunk(
                    d / f"chunk{chunk_id}.h5",
                    energies=[1.0 + 0.01 * i for i in range(100)],
                    xsec_weights=[0.02] * 100,
                    norm_count=100.0,
                    chunk_id=chunk_id,
                )
            single = _total_xsec_weight(d / "chunk0.h5")

            merge_hdf5_files([d / "chunk0.h5", d / "chunk1.h5"], d / "merged.h5")

            merged = _total_xsec_weight(d / "merged.h5")
            self.assertAlmostEqual(single, 2.0)
            # The merged estimate equals the per-chunk estimate; before the fix
            # this was 2x it. Event count still doubles.
            self.assertAlmostEqual(merged, single)
            _, events = read_events(d / "merged.h5")
            self.assertEqual(len(events), 200)

    def test_unequal_chunks_are_weighted_by_their_norm_count(self) -> None:
        # A 300-event chunk estimating sigma=3 and a 100-event chunk estimating
        # sigma=1 must combine to the sample-size-weighted mean, 2.5 — not the
        # unweighted mean 2.0, and not the sum 4.0.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _write_normalized_chunk(
                d / "big.h5",
                energies=[1.0] * 300,
                xsec_weights=[3.0 / 300] * 300,
                norm_count=300.0,
                chunk_id=0,
            )
            _write_normalized_chunk(
                d / "small.h5",
                energies=[1.0] * 100,
                xsec_weights=[1.0 / 100] * 100,
                norm_count=100.0,
                chunk_id=1,
            )

            merge_hdf5_files([d / "big.h5", d / "small.h5"], d / "merged.h5")

            self.assertAlmostEqual(_total_xsec_weight(d / "merged.h5"), 2.5)

    def test_single_input_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _write_normalized_chunk(
                d / "chunk0.h5",
                energies=[1.0] * 10,
                xsec_weights=[0.5] * 10,
                norm_count=10.0,
            )

            merge_hdf5_files([d / "chunk0.h5"], d / "merged.h5")

            self.assertAlmostEqual(_total_xsec_weight(d / "merged.h5"), 5.0)

    def test_merging_merged_files_stays_correct(self) -> None:
        # Associativity: the merged file carries the combined norm count, so
        # re-merging it with a third chunk still yields the same sigma.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            for chunk_id in range(3):
                _write_normalized_chunk(
                    d / f"chunk{chunk_id}.h5",
                    energies=[1.0] * 100,
                    xsec_weights=[0.02] * 100,
                    norm_count=100.0,
                    chunk_id=chunk_id,
                )

            merge_hdf5_files([d / "chunk0.h5", d / "chunk1.h5"], d / "ab.h5")
            merge_hdf5_files([d / "ab.h5", d / "chunk2.h5"], d / "abc.h5")
            merge_hdf5_files(
                [d / "chunk0.h5", d / "chunk1.h5", d / "chunk2.h5"], d / "flat.h5"
            )

            self.assertAlmostEqual(_total_xsec_weight(d / "abc.h5"), 2.0)
            self.assertAlmostEqual(
                _total_xsec_weight(d / "abc.h5"), _total_xsec_weight(d / "flat.h5")
            )

    def test_merged_file_records_the_combined_norm_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _write_normalized_chunk(
                d / "a.h5", energies=[1.0] * 5, xsec_weights=[1.0] * 5, norm_count=100.0
            )
            _write_normalized_chunk(
                d / "b.h5",
                energies=[1.0] * 5,
                xsec_weights=[1.0] * 5,
                norm_count=250.0,
                chunk_id=1,
            )

            merge_hdf5_files([d / "a.h5", d / "b.h5"], d / "merged.h5")

            metadata, _ = read_events(d / "merged.h5")
            self.assertAlmostEqual(float(metadata["xsec_norm_count"]), 350.0)

    def test_stub_files_without_norm_count_are_not_rescaled(self) -> None:
        # Stub output carries placeholder weights of 1.0, not cross sections;
        # rescaling them would be meaningless.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            for chunk_id in (0, 1):
                _write_normalized_chunk(
                    d / f"chunk{chunk_id}.h5",
                    energies=[1.0] * 4,
                    xsec_weights=[1.0] * 4,
                    norm_count=None,
                    chunk_id=chunk_id,
                )

            merge_hdf5_files([d / "chunk0.h5", d / "chunk1.h5"], d / "merged.h5")

            _, events = read_events(d / "merged.h5")
            self.assertEqual(len(events), 8)
            self.assertTrue(all(event["xsec_weight"] == 1.0 for event in events))
            metadata, _ = read_events(d / "merged.h5")
            self.assertNotIn("xsec_norm_count", metadata)

    def test_undeclared_inputs_are_skipped_not_fatal(self) -> None:
        """A few bad chunks must not cost the run its other 98.

        On the cluster this showed up as a merge refusing outright because two
        of a hundred chunks were still queued. Those two cannot share a
        normalization with the rest, so they are dropped with a warning and the
        merge proceeds.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            for i in range(3):
                _write_normalized_chunk(
                    d / f"real{i}.h5",
                    energies=[1.0] * 10,
                    xsec_weights=[0.1] * 10,
                    norm_count=10.0,
                    chunk_id=i,
                )
            _write_normalized_chunk(
                d / "stub.h5",
                energies=[1.0] * 10,
                xsec_weights=[1.0] * 10,
                norm_count=None,
                chunk_id=9,
            )

            inputs = [d / "real0.h5", d / "real1.h5", d / "stub.h5", d / "real2.h5"]
            with self.assertLogs("neutrino_factory.common_output", level="WARNING") as logs:
                merge_hdf5_files(inputs, d / "merged.h5")

            self.assertTrue(any("stub.h5" in line for line in logs.output))
            metadata, events = read_events(d / "merged.h5")
            # Only the three normalized chunks contribute.
            self.assertEqual(len(events), 30)
            self.assertAlmostEqual(_total_xsec_weight(d / "merged.h5"), 1.0)
            self.assertEqual(int(metadata["merged_file_count"]), 3)
            self.assertEqual(int(metadata["requested_file_count"]), 4)
            self.assertIn("stub.h5", str(metadata["skipped_inputs"]))

    def test_unreadable_inputs_are_skipped(self) -> None:
        # A chunk still being written, or truncated by a killed job, is not a
        # reason to lose the finished ones.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _write_normalized_chunk(
                d / "good.h5", energies=[1.0] * 10, xsec_weights=[0.2] * 10, norm_count=10.0
            )
            (d / "truncated.h5").write_bytes(b"\x89HDF\r\n\x1a\n not really an hdf5 file")

            with self.assertLogs("neutrino_factory.common_output", level="WARNING") as logs:
                merge_hdf5_files([d / "good.h5", d / "truncated.h5"], d / "merged.h5")

            self.assertTrue(any("truncated.h5" in line for line in logs.output))
            metadata, events = read_events(d / "merged.h5")
            self.assertEqual(len(events), 10)
            self.assertAlmostEqual(_total_xsec_weight(d / "merged.h5"), 2.0)
            self.assertEqual(int(metadata["requested_file_count"]), 2)

    def test_merge_fails_when_nothing_usable_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            (d / "a.h5").write_bytes(b"not hdf5")
            (d / "b.h5").write_bytes(b"not hdf5 either")

            with self.assertRaises(MergeError) as ctx:
                merge_hdf5_files([d / "a.h5", d / "b.h5"], d / "merged.h5")
            self.assertIn("No usable input files", str(ctx.exception))

    def test_version_mismatch_is_still_fatal(self) -> None:
        # Resilience covers partial runs, not physics errors: merging different
        # generator versions must keep failing.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _write_normalized_chunk(
                d / "a.h5", energies=[1.0], xsec_weights=[1.0], norm_count=10.0
            )
            _write_chunk(d / "b.h5", "genie", "R-3_06_00", "G18_10a_02_11a", 2)

            with self.assertRaises(MergeError) as ctx:
                merge_hdf5_files([d / "a.h5", d / "b.h5"], d / "merged.h5")
            self.assertIn("different generator versions", str(ctx.exception))
