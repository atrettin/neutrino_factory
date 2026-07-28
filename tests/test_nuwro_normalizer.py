from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.kinematics_reference import reference_kinematics, reference_lepton_p4
from neutrino_factory.common_output import read_events
from neutrino_factory.kinematics import FIELD_DEFAULTS, KINEMATIC_FIELDS, MISSING
from neutrino_factory.normalizers.nuwro import NuWroNormalizer


def _write_root_tree(
    path: Path, energies_mev, weights, qel, res, dis, coh, mec, out_counts=None
) -> None:
    """Write a synthetic ``treeout`` tree, all momenta in MeV as NuWro does.

    ``e/in`` holds [beam neutrino, struck nucleon]; ``e/out`` holds the primary
    outgoing lepton at index 0 and is jagged, so ``out_counts`` can be used to
    give an event no outgoing particles at all. The lepton follows the reference
    scatter from :func:`kinematics_reference.reference_lepton_p4`.
    """
    import awkward as ak
    import uproot

    energies = np.asarray(energies_mev, dtype=np.float64)
    count = len(energies)
    lepton = reference_lepton_p4(energies)
    if out_counts is None:
        out_counts = np.ones(count, dtype=np.int64)
    zeros = np.zeros(count, dtype=np.float64)

    def jagged(component: int):
        return ak.Array([
            [float(lepton[i, component])] + [0.0] * (int(n) - 1) if int(n) > 0 else []
            for i, n in enumerate(out_counts)
        ])

    with uproot.recreate(path) as f:
        f["treeout"] = {
            # Beam neutrino along +z with |p| = E; the struck nucleon is at rest
            # here since the normalizer does not read it.
            "e/in/in.t": np.column_stack([energies, zeros]),
            "e/in/in.x": np.column_stack([zeros, zeros]),
            "e/in/in.y": np.column_stack([zeros, zeros]),
            "e/in/in.z": np.column_stack([energies, zeros]),
            "e/out/out.t": jagged(0),
            "e/out/out.x": jagged(1),
            "e/out/out.y": jagged(2),
            "e/out/out.z": jagged(3),
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

    def test_normalize_root_derives_kinematics_in_gev(self) -> None:
        """NuWro stores MeV; the common format must come out in GeV."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0, 2500.0, 4000.0],
                weights=[1e-38] * 3,
                qel=[True, False, False],
                res=[False, True, False],
                dis=[False, False, True],
                coh=[False] * 3,
                mec=[False] * 3,
            )
            out_path = work_dir / "out.h5"

            NuWroNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            for event, energy_gev in zip(events, [1.0, 2.5, 4.0]):
                expected = reference_kinematics(energy_gev)
                for field in KINEMATIC_FIELDS:
                    self.assertAlmostEqual(event[field], expected[field], places=9, msg=field)

    def test_normalize_root_blanks_kinematics_without_outgoing_lepton(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[1000.0, 2000.0],
                weights=[1e-38, 1e-38],
                qel=[True, True],
                res=[False, False],
                dis=[False, False],
                coh=[False, False],
                mec=[False, False],
                out_counts=[1, 0],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 2}

            NuWroNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            # The event with an outgoing lepton is unaffected...
            self.assertAlmostEqual(events[0]["lepton_costheta"], 0.8)
            # ...the one without gets placeholders throughout, while the
            # non-kinematic columns stay valid.
            for field in KINEMATIC_FIELDS:
                self.assertEqual(events[1][field], FIELD_DEFAULTS[field], msg=field)
            self.assertAlmostEqual(events[1]["energy_gev"], 2.0)
            self.assertEqual(events[1]["interaction"], "qel")

    def test_normalize_root_blanks_bjorken_x_for_coherent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.root"
            _write_root_tree(
                root_path,
                energies_mev=[2000.0, 2000.0],
                weights=[1e-38, 1e-38],
                qel=[True, False],
                res=[False, False],
                dis=[False, False],
                coh=[False, True],
                mec=[False, False],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 2}

            NuWroNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertGreater(events[0]["bjorken_x"], 0.0)
            self.assertEqual(events[1]["bjorken_x"], MISSING)
            self.assertAlmostEqual(events[1]["q2_gev2"], reference_kinematics(2.0)["q2_gev2"])

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
