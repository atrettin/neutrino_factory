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
from neutrino_factory.normalizers.neut import NeutNormalizer


def _write_flat_root(
    path: Path,
    energies_gev,
    modes,
    *,
    sigma_avg: float = 1.0,
    flux_hist: str = "flux_numu",
    rate_hist: str = "evtrt_numu",
    rate_edges=None,
    lepton_pdgs=None,
    drop_lepton_branches: bool = False,
) -> None:
    """Write the file nf_flatten.C produces: an nf_neut tree + the two TH1Ds.

    The histograms are flat with a known integral ratio, so the flux-averaged
    cross section the normalizer derives from them is exactly ``sigma_avg``.

    Four-vectors follow the reference scatter from
    :func:`kinematics_reference.reference_lepton_p4` and, like nf_flatten.C's
    output, are already in GeV. ``lepton_pdgs`` entries of 0 mark an event where
    the flattener found no outgoing lepton.
    """
    import uproot

    edges = np.linspace(0.5, 5.0, 11)
    flux = np.ones(10, dtype=np.float64)
    rate = np.full(10, sigma_avg, dtype=np.float64)
    energies = np.array(energies_gev, dtype=np.float64)
    lepton = reference_lepton_p4(energies)
    if lepton_pdgs is None:
        lepton_pdgs = np.full(len(modes), 13, dtype=np.int32)
    lepton_pdgs = np.asarray(lepton_pdgs, dtype=np.int32)

    branches = {
        "mode": np.array(modes, dtype=np.int32),
        "pdgnu": np.full(len(modes), 14, dtype=np.int32),
        "enu_gev": energies,
        "totcrs": np.ones(len(modes), dtype=np.float64),
    }
    if not drop_lepton_branches:
        branches.update({
            # Beam neutrino along +z with |p| = E, as NEUT generates it.
            "nu_px_gev": np.zeros_like(energies),
            "nu_py_gev": np.zeros_like(energies),
            "nu_pz_gev": energies,
            "pdglep": lepton_pdgs,
            "lep_e_gev": lepton[:, 0],
            "lep_px_gev": lepton[:, 1],
            "lep_py_gev": lepton[:, 2],
            "lep_pz_gev": lepton[:, 3],
        })

    with uproot.recreate(path) as f:
        f["nf_neut"] = branches
        f[flux_hist] = (flux, edges)
        f[rate_hist] = (rate, edges if rate_edges is None else rate_edges)


def _base_task(**overrides) -> dict:
    task = {
        "run_name": "test_run",
        "chunk_id": 0,
        "seed": 42,
        "start_event": 0,
        "event_count": 3,
        "code_version": "5.7.0-nuint2024",
        "config_version": "default",
        "generator_version_id": "5.7.0-nuint2024+default",
    }
    task.update(overrides)
    return task


def _write_sidecar(work_dir: Path, gamma: float = 0.0) -> None:
    sidecar = {
        "probe": "numu",
        "target": "C12",
        "energy_range_gev": [0.5, 5.0],
        "seed": 42,
        "flux_config": {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": gamma,
        },
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")


class NeutNormalizerJsonTests(unittest.TestCase):
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
                "generator": "neut",
            }
            for i in range(3)
        ]
        payload = {
            "generator": "neut",
            "translated_config": {"probe": "numu", "target": "C12"},
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

            NeutNormalizer().normalize(json_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            # Stub mode doesn't compute a physical xsec_weight; schema default applies.
            self.assertEqual(events[0]["xsec_weight"], 1.0)

    def test_normalize_dispatches_json_on_json_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            json_path = self._make_stub_json(work_dir)
            out_path = work_dir / "out.h5"
            normalizer = NeutNormalizer()
            with patch.object(
                normalizer, "_normalize_json", wraps=normalizer._normalize_json
            ) as mock_json:
                normalizer.normalize(json_path, out_path, _base_task(), "local")
            mock_json.assert_called_once()


class NeutNormalizerRootTests(unittest.TestCase):
    def test_normalize_root_reads_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.5, 4.0], [1, 11, 26])
            out_path = work_dir / "out.h5"

            result = NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            self.assertEqual(result, str(out_path))
            metadata, events = read_events(out_path)
            self.assertEqual(len(events), 3)
            self.assertAlmostEqual(events[0]["energy_gev"], 1.0)
            self.assertAlmostEqual(events[2]["energy_gev"], 4.0)
            self.assertEqual(events[0]["probe"], "numu")
            self.assertEqual(events[0]["target"], "C12")
            self.assertEqual(metadata["generator"], "neut")
            # NEUT is unweighted, so the raw weight column carries no information.
            for event in events:
                self.assertEqual(event["weight"], 1.0)
                self.assertGreater(event["xsec_weight"], 0.0)
                self.assertTrue(np.isfinite(event["xsec_weight"]))

    def test_normalize_root_derives_kinematics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            energies = [1.0, 2.5, 4.0]
            _write_flat_root(root_path, energies, [1, 11, 26])
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            for event, energy in zip(events, energies):
                expected = reference_kinematics(energy)
                for field in KINEMATIC_FIELDS:
                    self.assertAlmostEqual(event[field], expected[field], places=9, msg=field)

    def test_normalize_root_blanks_bjorken_x_for_coherent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            # 1 = CC QE, 16 = CC coherent pi, 36 = NC coherent pi.
            _write_flat_root(root_path, [2.0, 2.0, 2.0], [1, 16, 36])
            out_path = work_dir / "out.h5"
            task = _base_task(event_count=3)

            NeutNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertGreater(events[0]["bjorken_x"], 0.0)
            self.assertEqual(events[1]["bjorken_x"], MISSING)
            self.assertEqual(events[2]["bjorken_x"], MISSING)
            # Only x is suppressed for coherent events.
            for event in events:
                self.assertAlmostEqual(event["q2_gev2"], reference_kinematics(2.0)["q2_gev2"])
                self.assertAlmostEqual(event["inelasticity_y"], 0.5)

    def test_normalize_root_blanks_kinematics_when_no_lepton_was_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            # pdglep = 0 is how nf_flatten.C reports "no outgoing lepton".
            _write_flat_root(root_path, [1.0, 2.0], [1, 1], lepton_pdgs=[13, 0])
            out_path = work_dir / "out.h5"
            task = _base_task(event_count=2)

            NeutNormalizer().normalize(root_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertAlmostEqual(events[0]["lepton_costheta"], 0.8)
            for field in KINEMATIC_FIELDS:
                self.assertEqual(events[1][field], FIELD_DEFAULTS[field], msg=field)
            # Non-kinematic columns are unaffected.
            self.assertAlmostEqual(events[1]["energy_gev"], 2.0)
            self.assertEqual(events[1]["interaction"], "qel")

    def test_normalize_root_raises_on_flat_file_without_lepton_branches(self) -> None:
        """A flat file from a pre-kinematics nf_flatten.C must fail loudly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.0], [1, 11], drop_lepton_branches=True)
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            self.assertIn("nf_flatten.C", str(ctx.exception))

    def test_mode_maps_to_every_interaction_category(self) -> None:
        # One representative NEUT mode per common-output category, plus an
        # unmapped one (15 = CC diffractive pi) that must land in "other".
        modes = [1, 51, 2, 12, 34, 21, 46, 16, 36, 15]
        expected = [
            "qel", "qel", "mec", "res", "res", "dis", "dis", "coh", "coh", "other",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0] * len(modes), modes)
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["interaction"] for e in events], expected)

    def test_antineutrino_modes_are_negated(self) -> None:
        # NEUT negates the mode for antineutrinos; the mapping keys on |mode|.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.0], [-1, -16])
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["interaction"] for e in events], ["qel", "coh"])

    def test_xsec_weight_matches_hand_derivation_for_flat_flux(self) -> None:
        """Flat flux and flat histograms make every weight the same number.

        With gamma = 0 the normalized flux density is 1 / (emax - emin)
        everywhere, so xsec_weight reduces to sigma_avg * (emax - emin) / N.
        sigma_avg itself is fixed by the flux/evtrt histograms written above:
        their bin contents are 1.0 and 0.75, so the ratio of integrals is 0.75.
        """
        sigma_avg = 0.75
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, gamma=0.0)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.0, 3.0], [1, 1, 1], sigma_avg=sigma_avg)
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            expected = sigma_avg * (5.0 - 0.5) / 3
            for event in events:
                self.assertAlmostEqual(event["xsec_weight"], expected, places=9)

    def test_normalize_root_event_ids_start_at_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.0], [1, 11])
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(
                root_path, out_path, _base_task(start_event=100, event_count=2), "local"
            )

            _, events = read_events(out_path)
            self.assertEqual([e["event_id"] for e in events], [100, 101])

    def test_normalize_root_raises_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0], [1])
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError):
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

    def test_normalize_root_raises_without_normalization_histograms(self) -> None:
        # NEUT only writes flux/evtrt when sampling a flux histogram; without
        # them there is no cross section to normalize with.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0], [1], rate_hist="something_else")
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            self.assertIn("evtrt_numu", str(ctx.exception))

    def test_normalize_root_raises_on_mismatched_histogram_binning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(
                root_path, [1.0], [1], rate_edges=np.linspace(0.5, 6.0, 11)
            )
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            self.assertIn("binning", str(ctx.exception))

    def test_normalize_root_raises_on_unflattened_output(self) -> None:
        # Handing the normalizer NEUT's native NeutVect file (no nf_neut tree)
        # must name the missing flatten step rather than fail obscurely.
        import uproot

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.neut.root"
            with uproot.recreate(root_path) as f:
                f["neuttree"] = {"something": np.array([1.0])}
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            self.assertIn("nf-neut-flatten", str(ctx.exception))

    def test_normalize_dispatches_root_on_root_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0], [1])
            out_path = work_dir / "out.h5"
            normalizer = NeutNormalizer()
            with patch.object(
                normalizer, "_normalize_root", wraps=normalizer._normalize_root
            ) as mock_root:
                normalizer.normalize(root_path, out_path, _base_task(), "local")
            mock_root.assert_called_once()


if __name__ == "__main__":
    unittest.main()
