from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.kinematics_reference import reference_kinematics, reference_lepton_p4
from neutrino_factory.common_output import read_events
from neutrino_factory.kinematics import KINEMATIC_FIELDS, MISSING
from neutrino_factory.normalizers.genie import GenieNormalizer
from neutrino_factory.translators.genie import GENIE_UNITS_CM2, XSEC_SCALE


def _write_gst_root(
    path: Path, energies_gev, weights, qel, res, dis, coh, mec, lepton_p4=None, cc=None
) -> None:
    """Write a synthetic ``gst`` tree.

    Unless ``lepton_p4`` is given, each event gets the reference scatter defined
    by :func:`kinematics_reference.reference_lepton_p4`: a beam neutrino along +z
    with |p| = E, and an outgoing lepton at E_l = E/2 with (px, pz) = (0.3E, 0.4E).
    """
    import uproot

    energies = np.array(energies_gev, dtype=np.float64)
    lepton = np.asarray(reference_lepton_p4(energies) if lepton_p4 is None else lepton_p4,
                        dtype=np.float64)

    with uproot.recreate(path) as f:
        f["gst"] = {
            "Ev": energies,
            "pxv": np.zeros_like(energies),
            "pyv": np.zeros_like(energies),
            "pzv": energies,
            "El": lepton[:, 0],
            "pxl": lepton[:, 1],
            "pyl": lepton[:, 2],
            "pzl": lepton[:, 3],
            "wght": np.array(weights, dtype=np.float64),
            # Charged current unless the test says otherwise; gst carries the
            # current independently of the interaction-class flags.
            "cc": np.array(np.ones(len(energies)) if cc is None else cc, dtype=np.bool_),
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


def _write_input_flux(
    work_dir: Path, edges, counts, name: str = "spectrum"
) -> Path:
    """Write a stand-in for the ``input-flux.root`` gevgen drops in its work dir.

    Bin contents are per-bin *integrals* (gevgen fills the histogram with entry
    counts), matching what the normalizer expects to convert back to a density.
    """
    import uproot

    path = work_dir / "input-flux.root"
    with uproot.recreate(path) as f:
        f[name] = (np.asarray(counts, dtype=np.float64), np.asarray(edges, dtype=np.float64))
    return path


def _write_sidecar(
    work_dir: Path,
    software_root: Path,
    emin_gev: float = 0.5,
    emax_gev: float = 10.0,
    probe: str = "numu",
    probe_pdg: int = 14,
) -> None:
    sidecar = {
        "probe": probe,
        "probe_pdg": probe_pdg,
        "target": "Ar40",
        "target_pdg": 1000180400,
        "energy_range_gev": [emin_gev, emax_gev],
        "events": 3,
        "seed": 42,
        "flux_model": "power_law",
        "flux_config": {
            "type": "power_law",
            "particle": probe,
            "emin_gev": emin_gev,
            "emax_gev": emax_gev,
            "gamma": 0.0,
        },
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        # Written by GenieTranslator.translate from storage.software_root; the
        # spline lookup for xsec_weight uses this, not NF_SOFTWARE_ROOT.
        "software_root": str(software_root),
    }
    (work_dir / "translated_config.json").write_text(json.dumps(sidecar), encoding="utf-8")
    # The normalizer divides events by the flux gevgen actually sampled, read
    # back from input-flux.root — never by the config flux. The sidecar's
    # gamma=0.0 flux corresponds to a flat histogram here.
    nbins = 100
    edges = np.linspace(emin_gev, emax_gev, nbins + 1)
    _write_input_flux(work_dir, edges, np.full(nbins, (emax_gev - emin_gev) / nbins))


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
                "is_cc": True,
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
        self._software_root = Path(self._software_root_dir.name)
        _write_fake_xsecs_xml(self._software_root, xsec_internal=1.0e-15)
        # Point NF_SOFTWARE_ROOT somewhere with no splines: the spline lookup must
        # succeed purely from the config-derived root carried in the sidecar.
        self._env_patch = patch.dict(
            os.environ, {"NF_SOFTWARE_ROOT": str(self._software_root / "not-the-config-root")}
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._software_root_dir.cleanup()

    def test_normalize_gst_root_reads_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
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
            _write_sidecar(work_dir, self._software_root)
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
            _write_sidecar(work_dir, self._software_root)
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
            _write_sidecar(work_dir, self._software_root)
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

    def test_is_cc_comes_from_the_cc_branch_not_the_class_flags(self) -> None:
        # Every event here is "qel", which in gst spans CCQE and NC elastic
        # alike: only the cc branch separates them.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[1.0, 2.0, 3.0],
                weights=[1.0] * 3,
                qel=[True] * 3,
                res=[False] * 3,
                dis=[False] * 3,
                coh=[False] * 3,
                mec=[False] * 3,
                cc=[True, False, True],
            )
            out_path = work_dir / "out.h5"

            GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["is_cc"] for e in events], [True, False, True])
            self.assertEqual([e["interaction"] for e in events], ["qel"] * 3)

    def test_normalize_gst_root_derives_kinematics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            gst_path = work_dir / "events.gst.root"
            energies = [1.0, 2.5, 4.0]
            _write_gst_root(
                gst_path,
                energies_gev=energies,
                weights=[1.0] * 3,
                qel=[True, False, False],
                res=[False, True, False],
                dis=[False, False, True],
                coh=[False] * 3,
                mec=[False] * 3,
            )
            out_path = work_dir / "out.h5"

            GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            for event, energy in zip(events, energies):
                expected = reference_kinematics(energy)
                for field in KINEMATIC_FIELDS:
                    self.assertAlmostEqual(event[field], expected[field], places=9, msg=field)

    def test_derived_kinematics_agree_with_genie_native_branches(self) -> None:
        """Pin our definitions to GENIE's own.

        The gst tree precomputes Q2/x/y/cthl. We deliberately recompute them from
        the four-vectors instead (so every generator uses one formula), which is
        only safe if the two agree. Here the native branches are filled with the
        hand-derived values for the reference scatter and compared against what
        the normalizer produces.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            gst_path = work_dir / "events.gst.root"
            energies = np.array([1.0, 2.5, 4.0])
            _write_gst_root(
                gst_path,
                energies_gev=energies,
                weights=[1.0] * 3,
                qel=[False] * 3,
                res=[False] * 3,
                dis=[True] * 3,
                coh=[False] * 3,
                mec=[False] * 3,
            )
            native = {
                "Q2": np.array([reference_kinematics(e)["q2_gev2"] for e in energies]),
                "x": np.array([reference_kinematics(e)["bjorken_x"] for e in energies]),
                "y": np.array([reference_kinematics(e)["inelasticity_y"] for e in energies]),
                "cthl": np.array([reference_kinematics(e)["lepton_costheta"] for e in energies]),
            }
            out_path = work_dir / "out.h5"

            GenieNormalizer().normalize(gst_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            for index, event in enumerate(events):
                self.assertAlmostEqual(event["q2_gev2"], native["Q2"][index], places=9)
                self.assertAlmostEqual(event["bjorken_x"], native["x"][index], places=9)
                self.assertAlmostEqual(event["inelasticity_y"], native["y"][index], places=9)
                self.assertAlmostEqual(event["lepton_costheta"], native["cthl"][index], places=9)

    def test_normalize_gst_root_blanks_bjorken_x_for_coherent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=[2.0, 2.0],
                weights=[1.0, 1.0],
                qel=[False, False],
                res=[False, False],
                dis=[True, False],
                coh=[False, True],
                mec=[False, False],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 2}

            GenieNormalizer().normalize(gst_path, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertGreater(events[0]["bjorken_x"], 0.0)
            self.assertEqual(events[1]["bjorken_x"], MISSING)
            # Everything else stays physical for the coherent event.
            self.assertAlmostEqual(events[1]["q2_gev2"], reference_kinematics(2.0)["q2_gev2"])
            self.assertAlmostEqual(events[1]["inelasticity_y"], 0.5)

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
            _write_sidecar(work_dir, self._software_root, emin_gev=emin, emax_gev=emax)
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

    def test_normalize_gst_root_raises_without_generated_flux(self) -> None:
        """Missing input-flux.root must fail loudly, never fall back to the config.

        The config flux is not what gevgen sampled (it bins, clips to the -e
        range, and for a TF1 input resamples with 100k entries), so silently
        substituting it produces a wrong normalization that looks physical.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            (work_dir / "input-flux.root").unlink()
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

            with self.assertRaises(RuntimeError) as ctx:
                GenieNormalizer().normalize(
                    gst_path, work_dir / "out.h5", _base_task(), "local"
                )
            self.assertIn("input-flux.root", str(ctx.exception))

    def test_normalize_gst_root_raises_when_the_probe_has_no_spline(self) -> None:
        """A probe the staged splines do not cover must fail loudly.

        The spline set staged here (like the shipped gxspl-NUsmall.xml) covers
        numu only; a nutau run would otherwise come back with every xsec_weight
        silently zero, which is indistinguishable from a physical result
        downstream.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(
                work_dir, self._software_root, probe="nutau", probe_pdg=16
            )
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

            with self.assertRaises(RuntimeError) as ctx:
                GenieNormalizer().normalize(
                    gst_path, work_dir / "out.h5", _base_task(), "local"
                )
            self.assertIn("16", str(ctx.exception))

    def test_normalize_gst_root_raises_without_software_root_in_sidecar(self) -> None:
        """A sidecar with no software_root must fail loudly.

        Falling back to NF_SOFTWARE_ROOT could resolve a *different* staged tune
        than the events were generated with and silently yield a wrong
        xsec_weight, so the missing key is an error.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, self._software_root)
            sidecar_path = work_dir / "translated_config.json"
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            del sidecar["software_root"]
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

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

            with self.assertRaises(RuntimeError) as ctx:
                GenieNormalizer().normalize(
                    gst_path, work_dir / "out.h5", _base_task(), "local"
                )
            self.assertIn("software_root", str(ctx.exception))

    def test_xsec_weight_uses_generated_flux_binning_not_the_config_flux(self) -> None:
        """Closure test against an independently computed flux-averaged xsec.

        Events are drawn the way GENIE draws them: energies uniform *within* the
        bins of the generated flux histogram, with per-bin counts proportional to
        that histogram. The histogram here is deliberately stepped and completely
        unlike the sidecar's flat config flux, so a normalizer that divided by the
        config flux (or re-binned onto its own grid) could not recover the answer.

        The reference value is derived independently of the implementation. Since
        w_i = <sigma>/(n * phi_hat(E_i)) and events land with density
        n * phi_hat(E) * sigma(E)/<sigma>, summing over the events in an energy
        interval estimates the *energy-integrated* cross section there:
        sum(w) -> integral of sigma(E) dE. With the constant fake spline that is
        just sigma * (emax - emin), and the estimator is exact (no Monte-Carlo
        error) because the per-bin event counts match the flux histogram exactly.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            emin, emax = 0.5, 5.0
            _write_sidecar(work_dir, self._software_root, emin_gev=emin, emax_gev=emax)

            # A stepped, variable-width generated flux with nothing in common with
            # the sidecar's flat 100-bin config flux.
            edges = np.array([0.5, 0.8, 1.5, 3.0, 5.0])
            counts = np.array([50.0, 10.0, 30.0, 10.0])  # per-bin integrals
            _write_input_flux(work_dir, edges, counts)

            # Events per bin proportional to the histogram; energies uniform in bin.
            energies = np.concatenate([
                np.linspace(lo, hi, int(n), endpoint=False) + 0.5 * (hi - lo) / int(n)
                for lo, hi, n in zip(edges[:-1], edges[1:], counts)
            ])
            n_events = len(energies)

            gst_path = work_dir / "events.gst.root"
            _write_gst_root(
                gst_path,
                energies_gev=energies,
                weights=np.ones(n_events),
                qel=np.ones(n_events, dtype=bool),
                res=np.zeros(n_events, dtype=bool),
                dis=np.zeros(n_events, dtype=bool),
                coh=np.zeros(n_events, dtype=bool),
                mec=np.zeros(n_events, dtype=bool),
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": n_events}

            GenieNormalizer().normalize(gst_path, out_path, task, "local")

            _, events = read_events(out_path)
            total = sum(e["xsec_weight"] for e in events)
            sigma_per_nucleon = 1.0e-15 * (XSEC_SCALE / GENIE_UNITS_CM2) / 40
            expected = sigma_per_nucleon * (edges[-1] - edges[0])
            self.assertAlmostEqual(total / expected, 1.0, places=9)

            # And the weights genuinely track the stepped flux: an event in the
            # low-density bin [1.5, 3.0) must weigh more than one in [0.5, 0.8).
            by_bin = {}
            for event in events:
                index = int(np.searchsorted(edges, event["energy_gev"], side="right")) - 1
                by_bin.setdefault(index, []).append(event["xsec_weight"])
            for weights in by_bin.values():
                self.assertTrue(np.allclose(weights, weights[0]))
            self.assertGreater(by_bin[2][0], by_bin[0][0])


if __name__ == "__main__":
    unittest.main()
