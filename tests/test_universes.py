from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from neutrino_factory import universes
from neutrino_factory.common_output import MergeError, merge_hdf5_files, write_common_hdf5
from neutrino_factory.config import ConfigError
from neutrino_factory.normalizers.nuwro import _universe_weights
from tests.helpers import job, run_config

NUWRO_JOB = {"generator": "nuwro", "code_version": "nuwro_25.11", "config_version": "default"}


def _block(**parameters) -> dict:
    return {"seed": 7, "count": 50, "parameters": parameters or {"qelNorm": {"sigma": 0.1}}}


class SamplingTests(unittest.TestCase):
    def test_throws_are_reproducible_and_shared_across_blocks(self) -> None:
        a = universes.draw_z(7, "qel_minerva_ff_scale", 10)
        b = universes.draw_z(7, "qel_minerva_ff_scale", 10)
        np.testing.assert_array_equal(a, b)
        self.assertFalse(np.array_equal(a, universes.draw_z(8, "qel_minerva_ff_scale", 10)))

    def test_each_parameter_has_its_own_stream(self) -> None:
        # Adding a parameter must not change another parameter's throws.
        alone = universes.resolve_universes(
            _block(mecNorm={"sigma": 0.2}), {"qel_axial_ff_set": 8}
        )
        together = universes.resolve_universes(
            _block(mecNorm={"sigma": 0.2}, qelNorm={"sigma": 0.1}), {"qel_axial_ff_set": 8}
        )
        self.assertEqual(alone["z"]["mecNorm"], together["z"]["mecNorm"])
        self.assertNotEqual(together["z"]["mecNorm"], together["z"]["qelNorm"])

    def test_raising_count_extends_the_existing_universes(self) -> None:
        np.testing.assert_array_equal(
            universes.draw_z(7, "qelNorm", 100)[:40], universes.draw_z(7, "qelNorm", 40)
        )

    def test_log_sampling_is_exact_in_log_space_and_positive(self) -> None:
        z = universes.draw_z(1, "mecNorm", 20000)
        values = universes.universe_values("mecNorm", 2.0, 0.3, True, z)
        np.testing.assert_allclose(np.log(values / 2.0), 0.3 * z, rtol=0, atol=1e-12)
        self.assertTrue(np.all(values > 0))
        self.assertAlmostEqual(float(np.median(values)), 2.0, delta=0.02)

    def test_linear_sampling(self) -> None:
        z = np.array([-1.0, 0.0, 2.0])
        np.testing.assert_allclose(
            universes.universe_values("delta_s", 0.0, 0.1, False, z), [-0.1, 0.0, 0.2]
        )

    def test_non_positive_linear_throw_of_a_positive_parameter_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "log: true"):
            universes.universe_values("mecNorm", 1.0, 1.0, False, np.array([-1.5]))

    def test_log_needs_a_positive_central(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive central"):
            universes.universe_values("qel_minerva_ff_scale", 0.0, 1.0, True, np.array([0.0]))

    def test_central_comes_from_generation(self) -> None:
        resolved = universes.resolve_universes(
            _block(qel_minerva_ff_scale={"sigma": 1.0}),
            {"qel_axial_ff_set": 8, "qel_minerva_ff_scale": 0.25},
        )
        spec = resolved["parameters"]["qel_minerva_ff_scale"]
        self.assertEqual((spec["central"], spec["sigma"], spec["log"]), (0.25, 1.0, False))
        np.testing.assert_allclose(
            resolved["values"]["qel_minerva_ff_scale"],
            0.25 + np.asarray(resolved["z"]["qel_minerva_ff_scale"]),
        )

    def test_parameter_without_effect_under_the_form_factor_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "no effect"):
            universes.resolve_universes(
                _block(qel_cc_axial_mass={"sigma": 100}),
                {"qel_axial_ff_set": 8, "qel_cc_axial_mass": 1030.0},
            )


class NormFactorTests(unittest.TestCase):
    def test_factors_apply_per_channel(self) -> None:
        flags = {
            "qel": np.array([1, 0, 0], bool), "res": np.array([0, 1, 0], bool),
            "dis": np.array([0, 0, 1], bool), "coh": np.zeros(3, bool),
            "mec": np.zeros(3, bool), "cc": np.array([1, 1, 0], bool),
        }
        values = {"qelNorm": np.array([2.0, 3.0]), "ncNorm": np.array([5.0, 7.0])}
        factors = universes.norm_factors(values, flags, antineutrino=False)
        np.testing.assert_array_equal(factors, [[2.0, 3.0], [1.0, 1.0], [5.0, 7.0]])

    def test_anty_norm_follows_the_probe(self) -> None:
        flags = {k: np.ones(2, bool) for k in ("qel", "res", "dis", "coh", "mec", "cc")}
        values = {"antyNorm": np.array([2.0])}
        np.testing.assert_array_equal(universes.norm_factors(values, flags, False), [[1.0], [1.0]])
        np.testing.assert_array_equal(universes.norm_factors(values, flags, True), [[2.0], [2.0]])


class ConfigValidationTests(unittest.TestCase):
    def _config(self, block) -> None:
        run_config([job(**NUWRO_JOB, nuwro={"universes": block})])

    def test_valid_block(self) -> None:
        self._config(_block(qel_minerva_ff_scale={"sigma": 1.0}, mecNorm={"sigma": 0.2, "log": True}))

    def test_broken_parameter_says_why(self) -> None:
        with self.assertRaisesRegex(ConfigError, "res_angrew"):
            self._config(_block(pion_axial_mass={"sigma": 0.1}))

    def test_unknown_parameter(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown parameter"):
            self._config(_block(not_a_param={"sigma": 0.1}))

    def test_central_is_not_configurable(self) -> None:
        with self.assertRaisesRegex(ConfigError, "central"):
            self._config(_block(qelNorm={"sigma": 0.1, "central": 1.1}))

    def test_seed_is_required(self) -> None:
        block = _block()
        del block["seed"]
        with self.assertRaisesRegex(ConfigError, "seed"):
            self._config(block)

    def test_bad_sigma(self) -> None:
        with self.assertRaisesRegex(ConfigError, "sigma"):
            self._config(_block(qelNorm={"sigma": 0}))

    def test_only_on_nuwro_jobs(self) -> None:
        with self.assertRaisesRegex(ConfigError, "only valid on a nuwro job"):
            run_config([job(nuwro={"universes": _block()})])


def _chunk(path: Path, n: int, weights: np.ndarray | None, universes_meta: dict | None) -> str:
    metadata = {
        "generator": "nuwro", "code_version": "nuwro_25.11", "config_version": "default",
        "run_name": "t", "chunk_id": 0, "seed": 1, "xsec_norm_count": float(n),
    }
    if universes_meta is not None:
        metadata["universes"] = universes_meta
    events = [
        {"event_id": i, "seed": 1, "energy_gev": 1.0, "is_cc": True, "interaction": "qel"}
        for i in range(n)
    ]
    return write_common_hdf5(path, metadata, events, universe_weights=weights)


class MergeTests(unittest.TestCase):
    META = {"seed": 7, "count": 2}

    def test_weights_are_concatenated_not_rescaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = _chunk(Path(tmp) / "a.h5", 2, np.array([[1.0, 2.0], [3.0, 4.0]]), self.META)
            b = _chunk(Path(tmp) / "b.h5", 1, np.array([[5.0, 6.0]]), self.META)
            out = merge_hdf5_files([a, b], Path(tmp) / "m.h5")
            with h5py.File(out, "r") as f:
                np.testing.assert_array_equal(
                    f["events/universe_weights"][()], [[1, 2], [3, 4], [5, 6]]
                )
                self.assertEqual(json.loads(f["metadata"].attrs["universes"]), self.META)

    def test_mismatched_universes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = _chunk(Path(tmp) / "a.h5", 1, np.ones((1, 2)), self.META)
            b = _chunk(Path(tmp) / "b.h5", 1, np.ones((1, 2)), {"seed": 8, "count": 2})
            with self.assertRaisesRegex(MergeError, "drawn differently"):
                merge_hdf5_files([a, b], Path(tmp) / "m.h5")

    def test_partial_universe_weights_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = _chunk(Path(tmp) / "a.h5", 1, np.ones((1, 2)), self.META)
            b = _chunk(Path(tmp) / "b.h5", 1, None, None)
            with self.assertRaisesRegex(MergeError, "carry universe weights"):
                merge_hdf5_files([a, b], Path(tmp) / "m.h5")

    def test_shape_must_match_the_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _chunk(Path(tmp) / "a.h5", 2, np.ones((3, 2)), self.META)


class NormalizerUniverseWeightsTests(unittest.TestCase):
    FLAGS = {
        "qel": np.array([1, 0, 0], bool), "res": np.array([0, 1, 0], bool),
        "dis": np.array([0, 0, 1], bool), "coh": np.zeros(3, bool),
        "mec": np.zeros(3, bool), "cc": np.ones(3, bool),
    }

    def _write(self, tmp: Path, parameters: dict, weights: np.ndarray | None) -> None:
        resolved = universes.resolve_universes(
            {"seed": 7, "count": 2, "parameters": parameters},
            {"qel_axial_ff_set": 8, "qel_minerva_ff_scale": 0.0},
        )
        (tmp / "universes.json").write_text(json.dumps(resolved))
        if weights is not None:
            import uproot

            with uproot.recreate(tmp / "universe_weights.root") as f:
                f["weights"] = {"weights": weights}

    def test_nuwro_weights_times_norm_factors(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            rew = np.array([[1.1, 0.9], [1.0, 1.0], [1.0, 1.0]])
            self._write(tmp, {"qel_minerva_ff_scale": {"sigma": 1.0}, "resNorm": {"sigma": 0.1}}, rew)
            meta, weights = _universe_weights(tmp, self.FLAGS, False, 3)
            res_factor = np.asarray(meta["values"]["resNorm"])
            np.testing.assert_allclose(weights[0], rew[0], rtol=1e-6)
            np.testing.assert_allclose(weights[1], res_factor, rtol=1e-6)
            np.testing.assert_allclose(weights[2], [1.0, 1.0])
            self.assertEqual(weights.dtype, np.float32)

    def test_non_finite_weights_fail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            rew = np.array([[np.nan, 1.0], [1.0, 1.0], [1.0, 1.2]])
            self._write(tmp, {"qel_minerva_ff_scale": {"sigma": 1.0}}, rew)
            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                _universe_weights(tmp, self.FLAGS, False, 3)

    def test_parameters_without_any_effect_fail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._write(tmp, {"qel_minerva_ff_scale": {"sigma": 1.0}}, np.ones((3, 2)))
            with self.assertRaisesRegex(RuntimeError, "carry no uncertainty"):
                _universe_weights(tmp, self.FLAGS, False, 3)

    def test_wrong_event_count_fails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._write(tmp, {"qel_minerva_ff_scale": {"sigma": 1.0}}, np.full((2, 2), 1.1))
            with self.assertRaisesRegex(RuntimeError, "shape"):
                _universe_weights(tmp, self.FLAGS, False, 3)


if __name__ == "__main__":
    unittest.main()
