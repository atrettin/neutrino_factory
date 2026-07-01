from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.common_output import read_events
from neutrino_factory.normalizers.genie import GenieNormalizer


def _write_gst_root(path: Path, energies_gev, weights, qel, res, dis, coh, mec) -> None:
    import uproot

    with uproot.recreate(path) as f:
        f["gst"] = {
            "Ev": np.array(energies_gev, dtype=np.float64),
            "wght": np.array(weights, dtype=np.float64),
            "qel": np.array(qel, dtype=np.bool_),
            "res": np.array(res, dtype=np.bool_),
            "dis": np.array(dis, dtype=np.bool_),
            "coh": np.array(coh, dtype=np.bool_),
            "mec": np.array(mec, dtype=np.bool_),
        }


def _base_task() -> dict:
    return {
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 42,
        "start_event": 0,
        "event_count": 3,
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "generator_version_id": "R-3_06_00+G18_10a_02_11a",
    }


def _write_sidecar(work_dir: Path) -> None:
    sidecar = {
        "probe": "numu",
        "target": "Ar40",
        "energy_range_gev": [0.5, 10.0],
        "events": 3,
        "seed": 42,
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")


class GenieNormalizerJsonTests(unittest.TestCase):
    def _make_stub_json(self, work_dir: Path) -> Path:
        events = [
            {
                "event_id": i,
                "seed": 42,
                "energy_gev": 1.0 + i * 0.5,
                "weight": 1.0,
                "interaction": "qel",
                "probe": "numu",
                "target": "Ar40",
                "generator": "genie",
                "generator_version_id": "R-3_06_00+G18_10a_02_11a",
            }
            for i in range(3)
        ]
        payload = {
            "generator": "genie",
            "translated_config": {"probe": "numu", "target": "Ar40"},
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

            result = GenieNormalizer().normalize(json_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            self.assertTrue(out_path.exists())
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertEqual(metadata["generator"], "genie")
            self.assertEqual(metadata["code_version"], "R-3_06_00")
            self.assertEqual(metadata["config_version"], "G18_10a_02_11a")
            self.assertEqual(metadata["generator_version_id"], "R-3_06_00+G18_10a_02_11a")
            self.assertEqual(events[0]["probe"], "numu")
            # Version info lives in metadata only, not per event.
            self.assertNotIn("generator_version_id", events[0])

    def test_normalize_dispatches_json_on_json_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"
            normalizer = GenieNormalizer()
            with patch.object(normalizer, "_normalize_json", wraps=normalizer._normalize_json) as mock_json:
                normalizer.normalize(json_path, out_path, _base_task(), "local")
            mock_json.assert_called_once()


class GenieNormalizerRootTests(unittest.TestCase):
    def test_normalize_gst_root_reads_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0, 2.5, 4.0],
                weights=[1.0, 0.8, 1.2],
                qel=[True, False, False],
                res=[False, True, False],
                dis=[False, False, False],
                coh=[False, False, False],
                mec=[False, False, False],
            )
            out_path = work_dir / "out.h5"

            result = GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertAlmostEqual(events[0]["energy_gev"], 1.0)
            self.assertAlmostEqual(events[1]["energy_gev"], 2.5)
            self.assertAlmostEqual(events[2]["energy_gev"], 4.0)
            self.assertAlmostEqual(events[1]["weight"], 0.8)
            self.assertEqual(events[0]["interaction"], "qel")
            self.assertEqual(events[1]["interaction"], "res")
            self.assertEqual(events[2]["interaction"], "other")
            self.assertEqual(events[0]["probe"], "numu")
            self.assertEqual(events[0]["target"], "Ar40")
            self.assertEqual(metadata["generator"], "genie")

    def test_normalize_gst_root_event_ids_start_at_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0, 2.0],
                weights=[1.0, 1.0],
                qel=[True, False],
                res=[False, True],
                dis=[False, False],
                coh=[False, False],
                mec=[False, False],
            )
            task = {**_base_task(), "start_event": 100, "event_count": 2}
            out_path = work_dir / "out.h5"

            GenieNormalizer().normalize(gst_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertEqual(events[0]["event_id"], 100)
            self.assertEqual(events[1]["event_id"], 101)

    def test_normalize_gst_root_raises_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0],
                weights=[1.0],
                qel=[True],
                res=[False],
                dis=[False],
                coh=[False],
                mec=[False],
            )
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError, msg="translated_config.json not found"):
                GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

    def test_normalize_dispatches_root_on_root_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0],
                weights=[1.0],
                qel=[True],
                res=[False],
                dis=[False],
                coh=[False],
                mec=[False],
            )
            out_path = work_dir / "out.h5"
            normalizer = GenieNormalizer()
            with patch.object(normalizer, "_normalize_gst_root", wraps=normalizer._normalize_gst_root) as mock_root:
                normalizer.normalize(gst_path, out_path, _base_task(), "local")
            mock_root.assert_called_once()

    def test_normalize_gst_root_all_interaction_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                weights=[1.0] * 6,
                qel=[True, False, False, False, False, False],
                res=[False, True, False, False, False, False],
                dis=[False, False, True, False, False, False],
                coh=[False, False, False, True, False, False],
                mec=[False, False, False, False, True, False],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 6}

            GenieNormalizer().normalize(gst_path, out_path, task, "local")

            _, events = read_events(out_path)
            interactions = [e["interaction"] for e in events]
            self.assertEqual(interactions, ["qel", "res", "dis", "coh", "mec", "other"])


if __name__ == "__main__":
    unittest.main()
