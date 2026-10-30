from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h5py
import numpy as np

from neutrino_factory import common_output
from neutrino_factory.common_output import (
    EVENT_NUMERIC_FIELDS,
    FILE_LEVEL_FIELDS,
    INTERACTION_CODES,
    MISSING_FLOAT_KEY,
    MISSING_FLOAT_NAN,
    NUMERIC_FIELD_SPECS,
    STORAGE_DTYPES,
    MergeError,
    merge_hdf5_files,
    read_events,
    read_interaction_column,
    write_common_hdf5,
)
from neutrino_factory.final_state import (
    COUNT_FIELDS,
    FINAL_STATE_FIELDS,
    MISSING_NATIVE_CODE,
    NATIVE_CODE_FIELD,
)
from neutrino_factory.final_state import FIELD_DEFAULTS as FINAL_STATE_DEFAULTS
from neutrino_factory.kinematics import FIELD_DEFAULTS, KINEMATIC_FIELDS
from tests.stored_values import assert_stored_close


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
    """Files written before the kinematic and final-state columns must stay usable."""

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
        # Strip the kinematic and final-state datasets to emulate a file from
        # the older schema.
        with h5py.File(path, "a") as handle:
            for field in (*KINEMATIC_FIELDS, *FINAL_STATE_FIELDS, NATIVE_CODE_FIELD):
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
                    np.testing.assert_equal(event[field], FIELD_DEFAULTS[field], err_msg=field)
                for field in FINAL_STATE_FIELDS:
                    np.testing.assert_equal(event[field], FINAL_STATE_DEFAULTS[field], err_msg=field)
                self.assertEqual(event[NATIVE_CODE_FIELD], MISSING_NATIVE_CODE)

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
                    np.testing.assert_equal(event[field], FIELD_DEFAULTS[field], err_msg=field)
                for field in FINAL_STATE_FIELDS:
                    np.testing.assert_equal(event[field], FINAL_STATE_DEFAULTS[field], err_msg=field)

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


class StringColumnLayoutTests(unittest.TestCase):
    """``interaction`` is an HDF5 enum; probe/target/generator are metadata attrs."""

    def _event(self, i: int, **fields) -> dict:
        event = {
            "event_id": i, "seed": 1, "energy_gev": 1.0, "is_cc": True,
            "interaction": "qel", "probe": "numu", "target": "Ar40", "generator": "genie",
        }
        event.update(fields)
        return event

    def test_interaction_is_an_enum_and_file_level_fields_are_attrs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            labels = ["qel", "res", "dis", "coh", "mec", "other"]
            events = [self._event(i, interaction=label) for i, label in enumerate(labels)]
            write_common_hdf5(path, {"generator": "genie"}, events)

            with h5py.File(path) as handle:
                dataset = handle["events"]["interaction"]
                self.assertEqual(h5py.check_enum_dtype(dataset.dtype), INTERACTION_CODES)
                self.assertEqual(dataset.dtype, np.int8)
                np.testing.assert_array_equal(
                    dataset[()], [INTERACTION_CODES[label] for label in labels]
                )
                for key in FILE_LEVEL_FIELDS:
                    self.assertNotIn(key, handle["events"])
                self.assertEqual(handle["metadata"].attrs["probe"], "numu")
                self.assertEqual(handle["metadata"].attrs["target"], "Ar40")
                self.assertEqual(handle["metadata"].attrs["generator"], "genie")

            _, read_back = read_events(path)
            self.assertEqual([e["interaction"] for e in read_back], labels)
            self.assertEqual({e["target"] for e in read_back}, {"Ar40"})

    def test_unknown_interaction_label_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "inclusive"):
                write_common_hdf5(
                    Path(tmpdir) / "bad.h5", {}, [self._event(0, interaction="inclusive")]
                )

    def test_missing_interaction_is_rejected(self) -> None:
        event = self._event(0)
        del event["interaction"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(KeyError, "interaction"):
                write_common_hdf5(Path(tmpdir) / "bad.h5", {}, [event])

    def test_mixed_targets_in_one_file_are_rejected(self) -> None:
        events = [self._event(0), self._event(1, target="C12")]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "target"):
                write_common_hdf5(Path(tmpdir) / "bad.h5", {}, events)

    def test_metadata_disagreeing_with_events_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "probe"):
                write_common_hdf5(Path(tmpdir) / "bad.h5", {"probe": "nue"}, [self._event(0)])

    def test_merging_unlike_initial_states_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            write_common_hdf5(d / "a.h5", {"generator": "genie"}, [self._event(0)])
            write_common_hdf5(d / "b.h5", {"generator": "genie"}, [self._event(1, probe="nue")])
            with self.assertRaisesRegex(ValueError, "probe"):
                merge_hdf5_files([d / "a.h5", d / "b.h5"], d / "merged.h5")

    def test_string_column_file_is_read_and_converted_by_merging(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            legacy = d / "legacy.h5"
            events = [self._event(0, interaction="dis"), self._event(1, interaction="coh")]
            write_common_hdf5(legacy, {"generator": "genie"}, events)
            # Rewrite into the old layout: variable-length string columns per event.
            with h5py.File(legacy, "a") as handle:
                group = handle["events"]
                del group["interaction"]
                group.create_dataset("interaction", data=["dis", "coh"], dtype=h5py.string_dtype())
                for key, value in (("probe", "numu"), ("target", "Ar40"), ("generator", "genie")):
                    group.create_dataset(key, data=[value] * 2, dtype=h5py.string_dtype())
                del handle["metadata"].attrs["probe"]
                del handle["metadata"].attrs["target"]

            _, read_back = read_events(legacy)
            self.assertEqual([e["interaction"] for e in read_back], ["dis", "coh"])
            self.assertEqual({e["probe"] for e in read_back}, {"numu"})

            merged = merge_hdf5_files([legacy], d / "merged.h5")
            with h5py.File(merged) as handle:
                self.assertIsNotNone(h5py.check_enum_dtype(handle["events"]["interaction"].dtype))
                self.assertNotIn("probe", handle["events"])
                self.assertEqual(handle["metadata"].attrs["probe"], "numu")
                self.assertEqual(handle["metadata"].attrs["target"], "Ar40")


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


def _rich_chunk(path: Path, n: int, *, offset: int = 0, norm_count: float = 10.0,
                matrix_columns: int | None = None, **overrides: object) -> str:
    """A chunk whose kinematic and final-state columns all carry real values."""
    rng = np.random.default_rng(offset + n)
    metadata: dict[str, object] = {
        "generator": "neut",
        "code_version": "5.7.0-nuint2024",
        "config_version": "default",
        "generator_version_id": "5.7.0-nuint2024+default",
        "run_name": "test_run",
        "chunk_id": offset,
        "seed": 1,
        "xsec_norm_count": norm_count,
    }
    events = []
    for i in range(n):
        event: dict[str, object] = {
            "event_id": offset + i,
            "seed": 1,
            "energy_gev": float(rng.uniform(0.1, 500.0)),
            "weight": 1.0,
            "is_cc": bool(i % 2),
            "xsec_weight": float(rng.uniform(0.0, 3.0)),
            "interaction": ("qel", "res", "dis")[i % 3],
            "probe": "numu",
            "target": "C12",
            "generator": "neut",
            NATIVE_CODE_FIELD: int(rng.integers(-60, 60)),
        }
        event.update({field: float(rng.uniform(-1.0, 50.0)) for field in KINEMATIC_FIELDS})
        event.update({
            field: int(rng.integers(0, 20)) if field in COUNT_FIELDS else float(rng.uniform(0, 9))
            for field in FINAL_STATE_FIELDS
        })
        event.update(overrides)
        events.append(event)
    matrix = None
    if matrix_columns is not None:
        metadata["universes"] = json.dumps({"z": [[0.0] * matrix_columns]})
        matrix = rng.uniform(0.5, 1.5, (n, matrix_columns))
    return write_common_hdf5(path, metadata, events, universe_weights=matrix)


class StorageFormatTests(unittest.TestCase):
    """Columns are stored compressed and narrowed, and read back in their spec dtype."""

    def test_columns_are_compressed_and_narrowed_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _rich_chunk(Path(tmpdir) / "chunk.h5", 50)
            with h5py.File(path) as handle:
                events = handle["events"]
                for key in (*EVENT_NUMERIC_FIELDS, "interaction"):
                    self.assertEqual(events[key].compression, "gzip", msg=key)
                    self.assertTrue(events[key].shuffle, msg=key)
                    expected = STORAGE_DTYPES.get(key, NUMERIC_FIELD_SPECS.get(key, (None,))[0])
                    if expected is not None:
                        self.assertEqual(events[key].dtype, np.dtype(expected), msg=key)
                self.assertEqual(events["q2_gev2"].dtype, np.float32)
                self.assertEqual(events["n_proton"].dtype, np.int16)
                self.assertEqual(events[NATIVE_CODE_FIELD].dtype, np.int32)
                for key in ("energy_gev", "weight", "xsec_weight"):
                    self.assertEqual(events[key].dtype, np.float64, msg=key)
                self.assertEqual(handle["metadata"].attrs[MISSING_FLOAT_KEY], MISSING_FLOAT_NAN)

    def test_values_round_trip_to_storage_precision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chunk.h5"
            metadata = {"generator": "neut", "code_version": "a", "config_version": "b"}
            event = {
                "event_id": 2**40, "seed": 2**33, "energy_gev": 0.1, "weight": 1.0,
                "is_cc": True, "xsec_weight": 1.0 / 3.0, "interaction": "qel",
                "probe": "numu", "target": "C12", "generator": "neut",
                "q2_gev2": 1.0 / 3.0, "lepton_costheta": -1.0, "n_pi_zero": 12,
                NATIVE_CODE_FIELD: MISSING_NATIVE_CODE,
            }
            write_common_hdf5(path, metadata, [event])
            _, (read,) = read_events(path)
            for key in ("event_id", "seed", "energy_gev", "xsec_weight", "n_pi_zero",
                        NATIVE_CODE_FIELD):
                self.assertEqual(read[key], event[key], msg=key)
            assert_stored_close(read["q2_gev2"], 1.0 / 3.0)
            self.assertNotEqual(read["q2_gev2"], 1.0 / 3.0)  # really stored as float32
            # -1 is a physical cosine and must survive.
            self.assertEqual(read["lepton_costheta"], -1.0)
            # Columns not given are NaN, not a number that could be binned.
            self.assertTrue(np.isnan(read["w_gev"]))
            self.assertTrue(np.isnan(read["leading_proton_costheta"]))

    def test_empty_file_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "empty.h5"
            write_common_hdf5(
                path,
                {"generator": "neut", "probe": "numu", "target": "C12"},
                [],
                universe_weights=np.zeros((0, 3)),
            )
            metadata, events = read_events(path)
            self.assertEqual(events, [])
            self.assertEqual(metadata["probe"], "numu")

    def test_value_overflowing_its_storage_dtype_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError) as ctx:
                _rich_chunk(Path(tmpdir) / "chunk.h5", 2, n_proton=40000)
            self.assertIn("n_proton", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                _rich_chunk(Path(tmpdir) / "chunk.h5", 2, hadronic_energy_gev=1e39)
            self.assertIn("hadronic_energy_gev", str(ctx.exception))


class LegacyPlaceholderTests(unittest.TestCase):
    """Files from before the NaN switch carry -1/-999, which reading maps to NaN."""

    def _legacy(self, path: Path) -> str:
        _rich_chunk(path, 3)
        with h5py.File(path, "a") as handle:
            del handle["metadata"].attrs[MISSING_FLOAT_KEY]
            events = handle["events"]
            events["q2_gev2"][1] = -1.0
            events["lepton_costheta"][0] = -1.0  # physical: must survive
            events["lepton_costheta"][1] = -999.0
            events["leading_proton_kinetic_energy_gev"][2] = -1.0
            events["leading_proton_costheta"][2] = -999.0
            events["n_proton"][2] = -1  # integer placeholder: unchanged
        return str(path)

    def test_legacy_placeholders_read_as_nan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, events = read_events(self._legacy(Path(tmpdir) / "legacy.h5"))
            self.assertTrue(np.isnan(events[1]["q2_gev2"]))
            self.assertEqual(events[0]["lepton_costheta"], -1.0)
            self.assertTrue(np.isnan(events[1]["lepton_costheta"]))
            self.assertTrue(np.isnan(events[2]["leading_proton_kinetic_energy_gev"]))
            self.assertTrue(np.isnan(events[2]["leading_proton_costheta"]))
            self.assertEqual(events[2]["n_proton"], -1)
            self.assertFalse(np.isnan(events[0]["q2_gev2"]))

    def test_stamped_files_are_not_rewritten(self) -> None:
        # Only unstamped files carry legacy placeholders; in a current file a
        # -1.0 is whatever the normalizer wrote.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _rich_chunk(Path(tmpdir) / "chunk.h5", 1, q2_gev2=-1.0)
            _, (event,) = read_events(path)
            self.assertEqual(event["q2_gev2"], -1.0)

    def test_merging_legacy_and_current_files_gives_nan_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            legacy = self._legacy(d / "legacy.h5")
            current = _rich_chunk(d / "current.h5", 2, offset=10, q2_gev2=float("nan"))
            merged = merge_hdf5_files([legacy, current], d / "merged.h5")
            with h5py.File(merged) as handle:
                self.assertEqual(handle["metadata"].attrs[MISSING_FLOAT_KEY], MISSING_FLOAT_NAN)
                q2 = handle["events"]["q2_gev2"][()]
            self.assertTrue(np.isnan(q2[1]))
            self.assertTrue(np.isnan(q2[3:]).all())
            self.assertFalse((q2 == -1.0).any())


class StreamingMergeTests(unittest.TestCase):
    """The streaming merge reproduces a plain concatenation, chunk by chunk."""

    def _expected(self, paths: list[str], counts: list[float]) -> dict[str, np.ndarray]:
        columns: dict[str, list[np.ndarray]] = {}
        for path, count in zip(paths, counts):
            with h5py.File(path) as handle:
                events = handle["events"]
                for key in (*EVENT_NUMERIC_FIELDS, "universe_weights"):
                    values = np.asarray(events[key][()], dtype=np.float64)
                    if key == "xsec_weight":
                        values = values * count / sum(counts)
                    columns.setdefault(key, []).append(values)
        return {key: np.concatenate(parts) for key, parts in columns.items()}

    def test_merge_equals_scaled_concatenation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            counts = [10.0, 30.0, 5.0]
            paths = [
                _rich_chunk(d / f"c{i}.h5", n, offset=100 * i, norm_count=count, matrix_columns=4)
                for i, (n, count) in enumerate(zip([7, 12, 0], counts))
            ]
            merged = merge_hdf5_files(paths, d / "merged.h5")
            expected = self._expected(paths, counts)
            with h5py.File(merged) as handle:
                events = handle["events"]
                self.assertEqual(int(handle["run"].attrs["event_count"]), 19)
                self.assertEqual(float(handle["metadata"].attrs["xsec_norm_count"]), 45.0)
                for key, values in expected.items():
                    np.testing.assert_allclose(
                        np.asarray(events[key][()], dtype=np.float64), values,
                        rtol=1e-15, err_msg=key,
                    )
                labels = read_interaction_column(events["interaction"])
            self.assertEqual(labels.tolist()[:3], ["qel", "res", "dis"])
            self.assertEqual(labels.tolist()[7:10], ["qel", "res", "dis"])

    def test_failed_merge_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            paths = [_rich_chunk(d / f"c{i}.h5", 3, offset=10 * i) for i in range(2)]
            calls = {"n": 0}
            real = common_output._read_event_columns

            def flaky(handle: h5py.File) -> dict[str, np.ndarray]:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("disk went away")
                return real(handle)

            with mock.patch.object(common_output, "_read_event_columns", flaky):
                with self.assertRaises(OSError):
                    merge_hdf5_files(paths, d / "merged.h5")
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["c0.h5", "c1.h5"])

    def test_weight_matrices_with_different_widths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            a = _rich_chunk(d / "a.h5", 2, matrix_columns=3)
            b = _rich_chunk(d / "b.h5", 2, offset=5, matrix_columns=3)
            with h5py.File(b, "a") as handle:
                del handle["events"]["universe_weights"]
                handle["events"].create_dataset("universe_weights", data=np.ones((2, 4)))
            with self.assertRaises(MergeError) as ctx:
                merge_hdf5_files([a, b], d / "merged.h5")
            self.assertIn("columns", str(ctx.exception))
