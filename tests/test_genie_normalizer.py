from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.common_output import read_events
from neutrino_factory.normalizers.genie import GenieNormalizer
from neutrino_factory.translators.genie import GENIE_UNITS_CM2, XSEC_SCALE


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


def _write_sidecar(work_dir: Path, emin_gev: float = 0.5, emax_gev: float = 10.0) -> None:
    sidecar = {
        "probe": "numu",
        "probe_pdg": 14,
        "target": "Ar40",
        "target_pdg": 1000180400,
        "energy_range_gev": [emin_gev, emax_gev],
        "events": 3,
        "seed": 42,
        "flux_model": "power_law",
        "flux_config": {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": emin_gev,
            "emax_gev": emax_gev,
            "gamma": 0.0,
        },
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _write_fake_xsecs_xml(software_root: Path, xsec_internal: float) -> None:
    """Stage a minimal xsecs.xml with a single constant-cross-section spline.

    Matches the real ``genie::XSecSplineList::SaveSplineList()`` format (see
    ``GenieTranslator.compute_xsec_weight``): one ``<spline name="...">`` whose
    name contains ``nu:<probe_pdg>;tgt:<target_pdg>;``, with two knots spanning
    the whole flux range so ``np.interp`` returns ``xsec_internal`` everywhere.
    """
    xsec_dir = software_root / "genie" / "genie_xsec" / "R-3_06_00" / "G18_10a_02_11a"
    xsec_dir.mkdir(parents=True, exist_ok=True)
    xml = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<genie_xsec_spline_list version="3.00" uselog="1">
  <genie_tune name="G18_10a_02_11a">
    <spline name="genie::FakeXSec/Default/nu:14;tgt:1000180400;N:2112;proc:Weak[CC],QES;" nknots="2">
	<knot> <E>    0.01000 </E> <xsec> {xsec_internal:.10e} </xsec> </knot>
	<knot> <E>   20.00000 </E> <xsec> {xsec_internal:.10e} </xsec> </knot>
    </spline>
  </genie_tune>
</genie_xsec_spline_list>
"""
    (xsec_dir / "xsecs.xml").write_text(xml, encoding="ISO-8859-1")


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
    def setUp(self) -> None:
        self._software_root_dir = tempfile.TemporaryDirectory()
        _write_fake_xsecs_xml(Path(self._software_root_dir.name), xsec_internal=1.0e-15)
        self._env_patch = patch.dict(os.environ, {"NF_SOFTWARE_ROOT": self._software_root_dir.name})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._software_root_dir.cleanup()

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
            for event in events:
                self.assertGreater(event["xsec_weight"], 0.0)
                self.assertTrue(np.isfinite(event["xsec_weight"]))

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

    def test_xsec_weight_matches_hand_derivation_for_flat_flux_and_constant_spline(self) -> None:
        """A flat (gamma=0) flux over [emin, emax] normalizes to a uniform
        density, so flux_hat(E) = 1/(emax-emin) everywhere. With the fake
        spline staged in setUp() (a constant cross section across the whole
        range), the flux-averaged total equals that constant directly, so
        xsec_weight collapses to a single value for every event -- and,
        crucially, that value already reflects the whole-nucleus -> per-nucleon
        division by Ar40's mass number (A=40, from target_pdg=1000180400), the
        subtlety documented in GenieTranslator.compute_xsec_weight."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            emin, emax = 0.5, 5.0
            _write_sidecar(work_dir, emin_gev=emin, emax_gev=emax)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0, 2.0, 3.0],
                weights=[1.0, 1.0, 1.0],
                qel=[True, True, True],
                res=[False, False, False],
                dis=[False, False, False],
                coh=[False, False, False],
                mec=[False, False, False],
            )
            out_path = work_dir / "out.h5"

            GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            n_events = 3
            mass_number = 40
            xsec_internal = 1.0e-15
            sigma_per_nucleon = xsec_internal * (XSEC_SCALE / GENIE_UNITS_CM2) / mass_number
            expected = sigma_per_nucleon * (emax - emin) / n_events
            for event in events:
                self.assertAlmostEqual(event["xsec_weight"], expected, places=6)


if __name__ == "__main__":
    unittest.main()
