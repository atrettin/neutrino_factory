from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.kinematics_reference import reference_kinematics, reference_lepton_p4
from neutrino_factory.common_output import read_events
from neutrino_factory.kinematics import KINEMATIC_FIELDS
from neutrino_factory.normalizers.gibuu import GiBUUNormalizer


def _write_roottuple(path: Path, lepIn_E, weight, evType, lepton_p4=None) -> None:
    """Write a synthetic ``RootTuple`` tree.

    Unless ``lepton_p4`` is given, each event gets the reference scatter defined
    by :func:`reference_lepton_p4`: a beam neutrino along +z with |p| = E, and an
    outgoing lepton at E_l = E/2 with (px, pz) = (0.3E, 0.4E).
    """
    import uproot

    energies = np.array(lepIn_E, dtype=np.float64)
    lepton = np.asarray(reference_lepton_p4(energies) if lepton_p4 is None else lepton_p4,
                        dtype=np.float64)

    with uproot.recreate(path) as f:
        f["RootTuple"] = {
            "lepIn_E": energies,
            "lepIn_Px": np.zeros_like(energies),
            "lepIn_Py": np.zeros_like(energies),
            "lepIn_Pz": energies,
            "lepOut_E": lepton[:, 0],
            "lepOut_Px": lepton[:, 1],
            "lepOut_Py": lepton[:, 2],
            "lepOut_Pz": lepton[:, 3],
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


def _part(work_dir: Path, index: int = 1, current: str = "cc") -> Path:
    """Path of one run's output inside its pass directory (created on demand)."""
    pass_dir = work_dir / current
    pass_dir.mkdir(parents=True, exist_ok=True)
    return pass_dir / f"EventOutput.Pert.{index:08d}.root"


def _write_sidecar(work_dir: Path, num_runs: int = 1, currents=("cc",)) -> None:
    sidecar = {
        "beam_particle": "numu",
        "nucleus": "C12",
        "energy_range_gev": [0.5, 5.0],
        "seed": 42,
        "num_runs": num_runs,
        "current": "inclusive" if len(currents) > 1 else currents[0],
        "gibuu_passes": [
            {"current": current, "jobcard": "! jobcard"} for current in currents
        ],
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
                "is_cc": True,
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
            root_path = _part(work_dir)
            _write_roottuple(
                root_path,
                lepIn_E=[1.0, 2.5, 4.0],
                weight=[1.0, 0.8, 1.2],
                evType=[1, 2, 34],
            )
            out_path = work_dir / "out.h5"

            result = GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")

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

    def test_normalize_root_derives_kinematics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = _part(work_dir)
            _write_roottuple(
                root_path, lepIn_E=[1.0, 2.5, 4.0], weight=[1.0] * 3, evType=[1, 2, 34]
            )
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            for event, energy in zip(events, [1.0, 2.5, 4.0]):
                expected = reference_kinematics(energy)
                for field in KINEMATIC_FIELDS:
                    self.assertAlmostEqual(event[field], expected[field], places=9, msg=field)

    def test_normalize_root_xsec_weight_matches_hand_derivation_for_flat_flux(self) -> None:
        # Flat power-law flux (gamma=0) over [0.5, 5.0] GeV: the unit-normalized
        # flux density is constant at 1/(emax-emin), so the GiBUU recipe
        # xsec_weight = raw_weight / (num_runs * flux_hat) collapses to
        # raw_weight * (emax - emin) / num_runs (num_runs = 1 in the sidecar).
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = _part(work_dir)
            raw = [1.0, 0.8, 1.2]
            _write_roottuple(
                root_path,
                lepIn_E=[1.0, 2.5, 4.0],
                weight=raw,
                evType=[1, 2, 34],
            )
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            width = 5.0 - 0.5
            for event, w in zip(events, raw):
                self.assertAlmostEqual(event["xsec_weight"], w * width, places=4)

    def test_normalize_root_event_ids_start_at_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = _part(work_dir)
            _write_roottuple(root_path, lepIn_E=[1.0, 2.0], weight=[1.0, 1.0], evType=[1, 2])
            task = {**_base_task(), "start_event": 100, "event_count": 2}
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(work_dir, out_path, task, "local")

            _, events = read_events(out_path)
            self.assertEqual(events[0]["event_id"], 100)
            self.assertEqual(events[1]["event_id"], 101)

    def test_normalize_root_interaction_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = _part(work_dir)
            _write_roottuple(
                root_path,
                lepIn_E=[1.0] * 6,
                weight=[1.0] * 6,
                evType=[1, 2, 31, 34, 35, 100],
            )
            out_path = work_dir / "out.h5"
            task = {**_base_task(), "event_count": 6}

            GiBUUNormalizer().normalize(work_dir, out_path, task, "local")

            _, events = read_events(out_path)
            interactions = [e["interaction"] for e in events]
            self.assertEqual(interactions, ["qel", "res", "res", "dis", "mec", "other"])

    def test_normalize_root_raises_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            root_path = _part(work_dir)
            _write_roottuple(root_path, lepIn_E=[1.0], weight=[1.0], evType=[1])
            out_path = work_dir / "out.h5"

            with self.assertRaises(RuntimeError):
                GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")

    def test_normalize_dispatches_root_on_a_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir)
            root_path = _part(work_dir)
            _write_roottuple(root_path, lepIn_E=[1.0], weight=[1.0], evType=[1])
            out_path = work_dir / "out.h5"
            normalizer = GiBUUNormalizer()
            with patch.object(normalizer, "_normalize_root", wraps=normalizer._normalize_root) as mock_root:
                normalizer.normalize(work_dir, out_path, _base_task(), "local")
            mock_root.assert_called_once()


class GiBUUInclusivePassTests(unittest.TestCase):
    """An inclusive GiBUU run is two passes, one per weak current.

    GiBUU's RootTuple carries no per-event current, so the flag comes from the
    pass directory an event was read from — which makes reading *both* passes,
    in a known order, part of the correctness of the flag.
    """

    def test_both_passes_are_read_and_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, currents=("cc", "nc"))
            _write_roottuple(_part(work_dir, current="cc"), [1.0, 2.0], [0.1, 0.1], [1, 1])
            _write_roottuple(_part(work_dir, current="nc"), [3.0], [0.1], [1])
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")

            _, events = read_events(out_path)
            self.assertEqual([e["energy_gev"] for e in events], [1.0, 2.0, 3.0])
            self.assertEqual([e["is_cc"] for e in events], [True, True, False])
            # event_id stays contiguous across the pass boundary.
            self.assertEqual([e["event_id"] for e in events], [0, 1, 2])

    def test_inclusive_total_is_the_sum_of_the_single_current_runs(self) -> None:
        # The two passes are separate estimates of sigma_CC and sigma_NC, so
        # concatenating them must give sigma_CC + sigma_NC exactly.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            totals = {}
            for currents in (("cc",), ("nc",), ("cc", "nc")):
                work_dir = root / "_".join(currents)
                work_dir.mkdir()
                _write_sidecar(work_dir, currents=currents)
                for current in currents:
                    _write_roottuple(
                        _part(work_dir, current=current), [1.0, 2.0], [0.25, 0.25], [1, 1]
                    )
                out_path = work_dir / "out.h5"
                GiBUUNormalizer().normalize(work_dir, out_path, _base_task(), "local")
                _, events = read_events(out_path)
                totals[currents] = sum(e["xsec_weight"] for e in events)

            self.assertAlmostEqual(
                totals[("cc", "nc")], totals[("cc",)] + totals[("nc",)]
            )

    def test_missing_pass_raises(self) -> None:
        # Only the CC pass ran: reporting it as an inclusive sample would drop
        # the NC cross section without any sign in the output.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, currents=("cc", "nc"))
            _write_roottuple(_part(work_dir, current="cc"), [1.0], [0.1], [1])

            with self.assertRaises(RuntimeError) as ctx:
                GiBUUNormalizer().normalize(
                    work_dir, work_dir / "out.h5", _base_task(), "local"
                )
            self.assertIn("nc", str(ctx.exception))


class GiBUUMultiRunOutputTests(unittest.TestCase):
    """GiBUU writes one file per run; every part must be read.

    With ``num_runs_SameEnergy = N`` GiBUU emits
    ``EventOutput.Pert.00000001.root`` .. ``...0000000N.root``. The per-event
    weights are divided by ``num_runs`` in ``compute_xsec_weight``, so reading
    only the first part would report sigma/N and drop the other runs' events.
    """

    def test_all_parts_are_read_and_concatenated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, num_runs=2)
            _write_roottuple(_part(work_dir, 1), [1.0, 2.0], [0.1, 0.1], [1, 1])
            _write_roottuple(_part(work_dir, 2), [3.0, 4.0, 5.0], [0.1] * 3, [1] * 3)
            out_path = work_dir / "out.h5"

            GiBUUNormalizer().normalize(
                work_dir, out_path, _base_task(), "local"
            )

            _, events = read_events(out_path)
            self.assertEqual(len(events), 5)
            self.assertEqual(
                sorted(event["energy_gev"] for event in events), [1.0, 2.0, 3.0, 4.0, 5.0]
            )
            # event_id stays contiguous across the part boundary.
            self.assertEqual([event["event_id"] for event in events], [0, 1, 2, 3, 4])

    def test_two_runs_report_the_same_cross_section_as_one(self) -> None:
        # Two runs produce twice the events at the same per-event weight, and
        # compute_xsec_weight divides by num_runs — so sigma must come out equal.
        with tempfile.TemporaryDirectory() as tmpdir:
            one = Path(tmpdir) / "one"
            two = Path(tmpdir) / "two"
            one.mkdir()
            two.mkdir()

            energies = [1.0, 2.0, 3.0, 4.0]
            _write_sidecar(one, num_runs=1)
            _write_roottuple(_part(one, 1), energies, [0.25] * 4, [1] * 4)
            GiBUUNormalizer().normalize(
                one, one / "out.h5", _base_task(), "local"
            )

            _write_sidecar(two, num_runs=2)
            for index in (1, 2):
                _write_roottuple(_part(two, index), energies, [0.25] * 4, [1] * 4)
            GiBUUNormalizer().normalize(
                two, two / "out.h5", _base_task(), "local"
            )

            _, one_events = read_events(one / "out.h5")
            _, two_events = read_events(two / "out.h5")
            self.assertEqual(len(two_events), 2 * len(one_events))
            self.assertAlmostEqual(
                sum(event["xsec_weight"] for event in one_events),
                sum(event["xsec_weight"] for event in two_events),
            )

    def test_missing_run_file_raises(self) -> None:
        # num_runs says 2 but only one part is present: reading it would still
        # divide by 2 and understate sigma by half, so fail instead.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _write_sidecar(work_dir, num_runs=2)
            _write_roottuple(_part(work_dir, 1), [1.0], [0.1], [1])

            with self.assertRaises(RuntimeError) as ctx:
                GiBUUNormalizer().normalize(
                    work_dir, work_dir / "out.h5", _base_task(), "local"
                )
            self.assertIn("num_runs=2", str(ctx.exception))

    def test_adapter_finds_parts_without_assuming_the_first_name(self) -> None:
        from neutrino_factory.normalizers.gibuu import pert_output_parts

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            for index in (2, 1, 3):
                _part(work_dir, index).write_text("", encoding="utf-8")
            parts = pert_output_parts(work_dir / "cc")
            self.assertEqual(
                [p.name for p in parts],
                [
                    "EventOutput.Pert.00000001.root",
                    "EventOutput.Pert.00000002.root",
                    "EventOutput.Pert.00000003.root",
                ],
            )
