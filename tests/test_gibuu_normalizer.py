from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.common_output import read_events
from neutrino_factory.normalizers.gibuu import GiBUUNormalizer


def _write_roottuple(path: Path, lepIn_E, weight, evType) -> None:
    import uproot

    with uproot.recreate(path) as f:
        f["RootTuple"] = {
            "lepIn_E": np.array(lepIn_E, dtype=np.float64),
            "weight": np.array(weight, dtype=np.float64),
            "evType": np.array(evType, dtype=np.int32),
        }


def _base_task() -> dict:
    return {
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 42,
        "start_event": 0,
        "event_count": 3,
        "code_version": "release2025",
        "config_version": "default",
        "generator_version_id": "release2025+default",
    }


def _write_sidecar(work_dir: Path) -> None:
    sidecar = {
        "beam_particle": "numu",
        "nucleus": "C12",
        "energy_range_gev": [0.5, 5.0],
        "seed": 42,
        "num_runs": 1,
        "flux_config": {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": 0.0,
        },
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")


class GiBUUNormalizerJsonTests(unittest.TestCase):
    def _make_stub_json(self, work_dir: Path) -> Path:
        events = [
            {
                "event_id": i,
                "seed": 42,
                "energy_gev": 1.0 + i * 0.5,
                "weight": 1.0,
                "interaction": "inclusive",
                "probe": "numu",
                "target": "C12",
                "generator": "gibuu",
            }
            for i in range(3)
        ]
        payload = {
            "generator": "gibuu",
            "translated_config": {"beam_particle": "numu", "nucleus": "C12"},
            "events": events,
        }
        path = work_dir / "stub.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_normalize_json_produces_hdf5(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"

            result = GiBUUNormalizer().normalize(json_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertEqual(metadata["generator"], "gibuu")
            self.assertEqual(metadata["code_version"], "release2025")
            self.assertEqual(metadata["config_version"], "default")
            self.assertEqual(events[0]["probe"], "numu")
            # Version info lives in metadata only, not per event.
            self.assertNotIn("generator_version_id", events[0])

    def test_normalize_dispatches_json_on_json_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"
            normalizer = GiBUUNormalizer()
            with patch.object(normalizer, "_normalize_json", wraps=normalizer._normalize_json) as mock_json:
                normalizer.normalize(json_path, out_path, _base_task(), "local")
            mock_json.assert_called_once()


class GiBUUNormalizerRootTests(unittest.TestCase):
    def test_normalize_root_reads_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            _write_roottuple(
                root_path,
                lepIn_E=[1.0, 2.5, 4.0],
                weight=[1.0, 0.8, 1.2],
                evType=[1, 2, 34],
            )
            out_path = work_dir / "out.h5"

            result = GiBUUNormalizer().normalize(root_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertAlmostEqual(events[0]["energy_gev"], 1.0)
            self.assertAlmostEqual(events[1]["energy_gev"], 2.5)
            self.assertAlmostEqual(events[2]["energy_gev"], 4.0)
            self.assertAlmostEqual(events[1]["weight"], 0.8)
            self.assertEqual(events[0]["interaction"], "qel")
            self.assertEqual(events[1]["interaction"], "res")
            self.assertEqual(events[2]["interaction"], "dis")
            self.assertEqual(events[0]["probe"], "numu")
            self.assertEqual(events[0]["target"], "C12")
            self.assertEqual(metadata["generator"], "gibuu")

    def test_normalize_root_xsec_weight_matches_hand_derivation_for_flat_flux(self) -> None:
        # Flat power-law flux (gamma=0) over [0.5, 5.0] GeV: the unit-normalized
        # flux density is constant at 1/(emax-emin), so the GiBUU recipe
        # xsec_weight = raw_weight / (num_runs * flux_hat) collapses to
        # raw_weight * (emax - emin) / num_runs (num_runs = 1 in the sidecar).
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            raw = [1.0, 0.8, 1.2]
            _write_roottuple(
                root_path,
                lepIn_E=[1.0, 2.5, 4.0],
                weight=raw,
                evType=[1, 2, 34],
            )
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            width = 5.0 - 0.5
            for event, w in zip(events, raw):
                self.assertAlmostEqual(event["xsec_weight"], w * width, places=4)

    def test_normalize_root_event_ids_start_at_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            _write_roottuple(root_path, lepIn_E=[1.0, 2.0], weight=[1.0, 1.0], evType=[1, 2])
            task = {**_base_task(), "start_event": 100, "event_count": 2}
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertEqual(events[0]["event_id"], 100)
            self.assertEqual(events[1]["event_id"], 101)

    def test_normalize_root_interaction_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            _write_roottuple(
                root_path,
                lepIn_E=[1.0] * 6,
                weight=[1.0] * 6,
                evType=[1, 2, 31, 34, 35, 100],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 6}

            GiBUUNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            interactions = [e["interaction"] for e in events]
            self.assertEqual(interactions, ["qel", "res", "res", "dis", "mec", "other"])

    def test_normalize_root_raises_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            _write_roottuple(root_path, lepIn_E=[1.0], weight=[1.0], evType=[1])
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError):
                GiBUUNormalizer().normalize(root_path, out_path, _base_task(), "local")

    def test_normalize_dispatches_root_on_root_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "EventOutput.Pert.00000001.root"
            _write_roottuple(root_path, lepIn_E=[1.0], weight=[1.0], evType=[1])
            out_path = work_dir / "out.h5"
            normalizer = GiBUUNormalizer()
            with patch.object(normalizer, "_normalize_root", wraps=normalizer._normalize_root) as mock_root:
                normalizer.normalize(root_path, out_path, _base_task(), "local")
            mock_root.assert_called_once()


if __name__ == "__main__":
    unittest.main()
