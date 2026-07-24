from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.common_output import read_events
from neutrino_factory.normalizers.nuwro import NuWroNormalizer


def _write_root_tree(path: Path, energies_mev, weights, qel, res, dis, coh, mec) -> None:
    import uproot

    in_t = np.array([[e, 0.0, 0.0, 0.0] for e in energies_mev], dtype=np.float64)
    with uproot.recreate(path) as f:
        f["treeout"] = {
            "e/in/in.t": in_t,
            "e/weight": np.array(weights, dtype=np.float64),
            "e/flag/flag.qel": np.array(qel, dtype=np.bool_),
            "e/flag/flag.res": np.array(res, dtype=np.bool_),
            "e/flag/flag.dis": np.array(dis, dtype=np.bool_),
            "e/flag/flag.coh": np.array(coh, dtype=np.bool_),
            "e/flag/flag.mec": np.array(mec, dtype=np.bool_),
        }


def _base_task() -> dict:
    return {
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 42,
        "start_event": 0,
        "event_count": 3,
        "code_version": "nuwro_25.11",
        "config_version": "default",
        "generator_version_id": "nuwro_25.11+default",
    }


def _write_sidecar(work_dir: Path, nucleus_p: int = 6, nucleus_n: int = 6) -> None:
    sidecar = {
        "beam_particle": "numu",
        "nucleus": "C12",
        "energy_range_gev": [0.5, 5.0],
        "seed": 42,
        "flux_config": {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": 0.0,
        },
        "nuwro_params": {"nucleus_p": nucleus_p, "nucleus_n": nucleus_n},
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")


class NuWroNormalizerJsonTests(unittest.TestCase):
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
                "generator": "nuwro",
            }
            for i in range(3)
        ]
        payload = {
            "generator": "nuwro",
            "translated_config": {"beam_particle": "numu", "nucleus": "C12"},
            "events": events,
        }
        path = work_dir / "stub.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_normalize_json_defaults_xsec_weight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"

            NuWroNormalizer().normalize(json_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            # Stub mode doesn't compute a physical xsec_weight; schema default applies.
            self.assertEqual(events[0]["xsec_weight"], 1.0)

    def test_normalize_dispatches_json_on_json_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"
            normalizer = NuWroNormalizer()
            with patch.object(normalizer, "_normalize_json", wraps=normalizer._normalize_json) as mock_json:
                normalizer.normalize(json_path, out_path, _base_task(), "local")
            mock_json.assert_called_once()


class NuWroNormalizerRootTests(unittest.TestCase):
    def test_normalize_root_reads_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0, 2500.0, 4000.0],
                weights=[1e-38, 1e-38, 1e-38],
                qel=[True, False, False],
                res=[False, True, False],
                dis=[False, False, True],
                coh=[False, False, False],
                mec=[False, False, False],
            )
            out_path = work_dir / "out.h5"

            result = NuWroNormalizer().normalize(root_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertAlmostEqual(events[0]["energy_gev"], 1.0)
            self.assertAlmostEqual(events[1]["energy_gev"], 2.5)
            self.assertAlmostEqual(events[2]["energy_gev"], 4.0)
            self.assertEqual(events[0]["interaction"], "qel")
            self.assertEqual(events[1]["interaction"], "res")
            self.assertEqual(events[2]["interaction"], "dis")
            self.assertEqual(events[0]["probe"], "numu")
            self.assertEqual(events[0]["target"], "C12")
            self.assertEqual(metadata["generator"], "nuwro")
            # Raw NuWro weight (constant) still preserved verbatim.
            self.assertAlmostEqual(events[0]["weight"], 1e-38)
            for event in events:
                self.assertGreater(event["xsec_weight"], 0.0)
                self.assertTrue(np.isfinite(event["xsec_weight"]))

    def test_xsec_weight_matches_hand_derivation_for_flat_flux(self) -> None:
        """A flat (gamma=0) flux normalizes to a uniform density over
        [emin, emax], so phi_hat(E) = 1 / (emax - emin) everywhere and the
        formula reduces to xsec_weight = raw_weight * 1e38 * (emax - emin)
        / N for every event, independent of energy. Raw NuWro weight is
        already per-target-nucleon (see compute_xsec_weight docstring), so
        there is no further division by nucleon count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, nucleus_p=6, nucleus_n=6)
            root_path = work_dir / "events.root"
            raw_weight = 2e-38
            _write_root_tree(
                root_path,
                energies_mev=[1000.0, 2000.0, 3000.0],
                weights=[raw_weight] * 3,
                qel=[True, True, True],
                res=[False, False, False],
                dis=[False, False, False],
                coh=[False, False, False],
                mec=[False, False, False],
            )
            out_path = work_dir / "out.h5"

            NuWroNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            n_events = 3
            emin, emax = 0.5, 5.0
            # Raw NuWro weight is already a per-target-nucleon quantity (see
            # compute_xsec_weight docstring) - no division by nucleon count.
            expected = raw_weight * 1e38 * (emax - emin) / n_events
            for event in events:
                self.assertAlmostEqual(event["xsec_weight"], expected, places=6)

    def test_normalize_root_event_ids_start_at_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0, 2000.0],
                weights=[1e-38, 1e-38],
                qel=[True, False],
                res=[False, True],
                dis=[False, False],
                coh=[False, False],
                mec=[False, False],
            )
            task = {**_base_task(), "start_event": 100, "event_count": 2}
            out_path = work_dir / "out.h5"

            NuWroNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertEqual(events[0]["event_id"], 100)
            self.assertEqual(events[1]["event_id"], 101)

    def test_normalize_root_raises_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0],
                weights=[1e-38],
                qel=[True],
                res=[False],
                dis=[False],
                coh=[False],
                mec=[False],
            )
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError):
                NuWroNormalizer().normalize(root_path, out_path, _base_task(), "local")

    def test_normalize_dispatches_root_on_root_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0],
                weights=[1e-38],
                qel=[True],
                res=[False],
                dis=[False],
                coh=[False],
                mec=[False],
            )
            out_path = work_dir / "out.h5"
            normalizer = NuWroNormalizer()
            with patch.object(normalizer, "_normalize_root", wraps=normalizer._normalize_root) as mock_root:
                normalizer.normalize(root_path, out_path, _base_task(), "local")
            mock_root.assert_called_once()


if __name__ == "__main__":
    unittest.main()
