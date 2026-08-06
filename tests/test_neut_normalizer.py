from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.kinematics_reference import (
    reference_kinematics,
    reference_lepton_p4,
    reference_nucleon_p4,
)
from neutrino_factory.common_output import RESONANT_PRIMARY_UNKNOWN, read_events
from neutrino_factory.kinematics import FIELD_DEFAULTS, KINEMATIC_FIELDS, MISSING
from neutrino_factory.normalizers.neut import FlatSchemaError, NeutNormalizer


def _write_flat_root(
    path: Path,
    energies_gev,
    modes,
    *,
    sigma_avg: float = 1.0,
    flux_hist: str = "flux_numu",
    rate_hist: str = "evtrt_numu",
    rate_edges=None,
    edges=None,
    flux_contents=None,
    lepton_pdgs=None,
    nucleon_counts=None,
    drop_lepton_branches: bool = False,
    drop_nucleon_branches: bool = False,
) -> None:
    """Write the file nf_flatten.C produces: an nf_neut tree + the two TH1Ds.

    ``evtrt`` is ``sigma_avg`` times ``flux`` bin by bin, so the flux-averaged
    cross section the normalizer derives from the two integrals is exactly
    ``sigma_avg`` whatever the binning. ``edges``/``flux_contents`` override the
    default flat spectrum on a uniform grid; ``flux_contents`` are per-bin
    integrals, the convention NeutAdapter writes and the normalizer undoes.

    Four-vectors follow the reference scatter from
    :func:`kinematics_reference.reference_lepton_p4` and, like nf_flatten.C's
    output, are already in GeV. ``lepton_pdgs`` entries of 0 mark an event where
    the flattener found no outgoing lepton.
    """
    import uproot

    edges = np.linspace(0.5, 5.0, 11) if edges is None else np.asarray(edges, np.float64)
    flux = (
        np.ones(len(edges) - 1, dtype=np.float64)
        if flux_contents is None
        else np.asarray(flux_contents, dtype=np.float64)
    )
    rate = sigma_avg * flux
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
    if not drop_nucleon_branches:
        # nf_flatten.C writes the *sum* over the struck initial-state nucleons,
        # so n_nuc = 2 (2p2h) is a pair four-vector, and n_nuc = 0 means it found
        # none and the four-vector is zeros.
        counts = (
            np.ones(len(modes), dtype=np.int32)
            if nucleon_counts is None
            else np.asarray(nucleon_counts, dtype=np.int32)
        )
        nucleon = reference_nucleon_p4(energies) * counts[:, None]
        branches.update({
            "n_nuc": counts,
            "pdgnuc": np.where(counts > 0, 2112, 0).astype(np.int32),
            "nuc_e_gev": nucleon[:, 0],
            "nuc_px_gev": nucleon[:, 1],
            "nuc_py_gev": nucleon[:, 2],
            "nuc_pz_gev": nucleon[:, 3],
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


def _write_sidecar(work_dir: Path, gamma: float = 0.0, probe: str = "numu") -> None:
    sidecar = {
        "probe": probe,
        "target": "C12",
        "energy_range_gev": [0.5, 5.0],
        "seed": 42,
        "flux_config": {
            "type": "power_law",
            "particle": probe,
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
                "is_cc": True,
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

    def test_normalize_root_raises_on_flat_file_without_nucleon_branches(self) -> None:
        """Likewise for a flat file predating the struck-nucleon branches.

        Blanking w_true_gev for the whole run instead would be indistinguishable
        from a run of genuinely nucleon-less events.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0, 2.0], [1, 11], drop_nucleon_branches=True)
            out_path = work_dir / "out.h5"

            with self.assertRaises(FlatSchemaError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            self.assertIn("nf_flatten.C", str(ctx.exception))
            self.assertIn("nuc_e_gev", str(ctx.exception))

    def test_resonant_primary_is_unknown_for_every_neut_event(self) -> None:
        """NEUT's single-pion modes lump the two mechanisms and it emits no flag.

        This negative is the reason the column exists as its own column rather
        than as a correction to `interaction`: for NEUT there is nothing to
        correct with.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [2.0] * 4, [1, 11, 21, 2])
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["interaction"] for e in events],
                             ["qel", "res", "dis", "mec"])
            for event in events:
                self.assertEqual(event["resonant_primary"], RESONANT_PRIMARY_UNKNOWN)

    def test_nucleon_count_drives_the_true_w(self) -> None:
        """n_nuc = 0 blanks it; n_nuc = 2 is the 2p2h pair, not a single nucleon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(
                root_path,
                [2.0, 2.0, 2.0],
                [1, 2, 16],
                nucleon_counts=[1, 2, 0],
            )
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            single = reference_kinematics(2.0)["w_true_gev"]
            self.assertAlmostEqual(events[0]["w_true_gev"], single, places=9)
            self.assertGreater(events[1]["w_true_gev"], single + 0.5)
            # Mode 16 is coherent: no nucleon, and the label blanks it as well.
            self.assertEqual(events[2]["w_true_gev"], MISSING)
            self.assertEqual(events[2]["w_gev"], MISSING)
            # The lepton-only W survives wherever there is a struck nucleon.
            for event in events[:2]:
                self.assertAlmostEqual(
                    event["w_gev"], reference_kinematics(2.0)["w_gev"], places=9
                )

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

    def test_is_cc_follows_the_mode_number_not_the_category(self) -> None:
        # CCQE (1) and NC elastic (51/52) share the "qel" category, and the mode
        # is negated for antineutrinos: the current is |mode| <= 30.
        modes = [1, 51, -1, -52, 16, 36]
        expected = [True, False, True, False, True, False]
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0] * len(modes), modes)
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["is_cc"] for e in events], expected)

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

    def test_xsec_weight_follows_the_stamped_flux_not_the_config(self) -> None:
        """The divisor is NEUT's own flux histogram, on its native binning.

        The config flux here is *flat* while the histogram NEUT stamped out
        falls steeply on an unequal-width (log) grid. If the normalizer rebuilt
        the flux from the config — as it used to — every event would come back
        with the same weight. It must instead divide by the piecewise-constant
        density NEUT actually sampled: contents are per-bin integrals, so the
        density in bin b is content_b / width_b, and two events in different
        bins must have weights in inverse ratio to those densities.
        """
        edges = np.geomspace(0.5, 5.0, 5)
        widths = np.diff(edges)
        # Per-bin integrals of a steeply falling density.
        density = np.array([80.0, 8.0, 0.8, 0.08])
        contents = density * widths

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, gamma=0.0)
            root_path = work_dir / "events.flat.root"
            # One event near the middle of the first and of the last bin.
            centers = np.sqrt(edges[:-1] * edges[1:])
            _write_flat_root(
                root_path,
                [centers[0], centers[-1]],
                [1, 1],
                sigma_avg=0.75,
                edges=edges,
                flux_contents=contents,
            )
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            ratio = events[1]["xsec_weight"] / events[0]["xsec_weight"]
            self.assertAlmostEqual(ratio, density[0] / density[-1], places=6)
            # And the absolute scale: sigma_avg / (N * phi_hat) with phi_hat the
            # unit-normalized density of the bin the event fell in.
            integral = float(np.sum(density * widths))
            self.assertAlmostEqual(
                events[0]["xsec_weight"], 0.75 * integral / (2 * density[0]), places=9
            )

    def test_xsec_weight_matches_hand_derivation_for_flat_flux(self) -> None:
        """Flat flux and flat histograms make every weight the same number.

        The stamped flux histogram here is flat on ten equal-width bins over
        [0.5, 5.0], so the normalized density is 1 / (emax - emin) everywhere and
        xsec_weight reduces to sigma_avg * (emax - emin) / N. sigma_avg itself is
        fixed by the flux/evtrt histograms written above: their bin contents are
        1.0 and 0.75, so the ratio of integrals is 0.75.
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
            message = str(ctx.exception)
            self.assertIn("evtrt_", message)
            # The error names what the file does hold, so the mismatch is visible.
            self.assertIn("something_else", message)

    def test_normalization_histograms_are_found_for_any_flavour(self) -> None:
        """NEUT names flux/evtrt after the beam, so the pair is found by prefix.

        neutroot2 formats them as "flux_%s"/"evtrt_%s" with its own flavour
        token ("numub" for a numubar beam, not "numubar"), and writes only the
        generic "fluxhisto"/"ratehisto" pair for a beam it has no token for.
        None of those spellings may change the normalization: the same events
        with the same histograms must give the same xsec_weight whatever the
        pair is called.
        """
        sigma_avg = 0.75
        expected = sigma_avg * (5.0 - 0.5) / 2
        naming = [
            ("numu", "flux_numu", "evtrt_numu"),
            ("numubar", "flux_numub", "evtrt_numub"),
            ("nue", "flux_nue", "evtrt_nue"),
            ("nuebar", "flux_nueb", "evtrt_nueb"),
            ("nutau", "fluxhisto", "ratehisto"),
        ]
        for probe, flux_hist, rate_hist in naming:
            with self.subTest(probe=probe):
                with tempfile.TemporaryDirectory() as tmpdir:
                    work_dir = Path(tmpdir)
                    _write_sidecar(work_dir, probe=probe)
                    root_path = work_dir / "events.flat.root"
                    _write_flat_root(
                        root_path,
                        [1.0, 2.0],
                        [1, 1],
                        sigma_avg=sigma_avg,
                        flux_hist=flux_hist,
                        rate_hist=rate_hist,
                    )
                    out_path = work_dir / "out.h5"

                    NeutNormalizer().normalize(
                        root_path, out_path, _base_task(event_count=2), "local"
                    )

                    _, events = read_events(out_path)
                    self.assertEqual([e["probe"] for e in events], [probe, probe])
                    for event in events:
                        self.assertAlmostEqual(event["xsec_weight"], expected, places=9)

    def test_generic_histograms_do_not_shadow_the_flavour_named_pair(self) -> None:
        """A real NEUT run writes both pairs; the flavour-named one is used.

        Verified against a real nuebar run in the NEUT 5.7.0 image: the output
        holds flux_nueb/evtrt_nueb *and* an identical fluxhisto/ratehisto copy.
        The generic pair must therefore not read as a second, ambiguous
        candidate.
        """
        import uproot

        sigma_avg = 0.75
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, probe="nuebar")
            root_path = work_dir / "events.flat.root"
            _write_flat_root(
                root_path,
                [1.0, 2.0],
                [1, 1],
                sigma_avg=sigma_avg,
                flux_hist="flux_nueb",
                rate_hist="evtrt_nueb",
            )
            edges = np.linspace(0.5, 5.0, 11)
            with uproot.update(root_path) as f:
                f["fluxhisto"] = (np.ones(len(edges) - 1), edges)
                f["ratehisto"] = (sigma_avg * np.ones(len(edges) - 1), edges)
            out_path = work_dir / "out.h5"

            NeutNormalizer().normalize(
                root_path, out_path, _base_task(event_count=2), "local"
            )

            _, events = read_events(out_path)
            expected = sigma_avg * (5.0 - 0.5) / 2
            for event in events:
                self.assertAlmostEqual(event["xsec_weight"], expected, places=9)

    def test_normalize_root_raises_on_ambiguous_normalization_histograms(self) -> None:
        # Two flux histograms leave the choice of normalization to a guess.
        import uproot

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = work_dir / "events.flat.root"
            _write_flat_root(root_path, [1.0], [1])
            edges = np.linspace(0.5, 5.0, 11)
            with uproot.update(root_path) as f:
                f["flux_nue"] = (np.ones(len(edges) - 1), edges)
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError) as ctx:
                NeutNormalizer().normalize(root_path, out_path, _base_task(), "local")
            message = str(ctx.exception)
            self.assertIn("flux_numu", message)
            self.assertIn("flux_nue", message)

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
