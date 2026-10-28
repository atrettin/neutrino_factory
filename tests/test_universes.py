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
        alone = universes.resolve_universes(_block(mecNorm={"sigma": 0.2}), {"mecNorm": 1.0})
        together = universes.resolve_universes(
            _block(mecNorm={"sigma": 0.2}, qelNorm={"sigma": 0.1}),
            {"mecNorm": 1.0, "qelNorm": 1.0},
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
        block = _block(qel_minerva_ff_scale={"sigma": 1.0})
        centrals = universes.nuwro_centrals(
            block["parameters"], {"qel_axial_ff_set": 8, "qel_minerva_ff_scale": 0.25}
        )
        resolved = universes.resolve_universes(block, centrals)
        spec = resolved["parameters"]["qel_minerva_ff_scale"]
        self.assertEqual((spec["central"], spec["sigma"], spec["log"]), (0.25, 1.0, False))
        np.testing.assert_allclose(
            resolved["values"]["qel_minerva_ff_scale"],
            0.25 + np.asarray(resolved["z"]["qel_minerva_ff_scale"]),
        )

    def test_parameter_without_effect_under_the_form_factor_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "no effect"):
            universes.nuwro_centrals(
                ["qel_cc_axial_mass"], {"qel_axial_ff_set": 8, "qel_cc_axial_mass": 1030.0}
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


RHO = [[1.0, 0.6], [0.6, 1.0]]


def _correlated(matrix=RHO, names=("qelNorm", "mecNorm"), count=50, **extra) -> dict:
    block = _block(**{name: {"sigma": 0.1} for name in names}, **extra)
    block["count"] = count
    block["correlations"] = [{"parameters": list(names), "matrix": matrix, "source": "x"}]
    return block


class CorrelationTests(unittest.TestCase):
    def test_throws_get_the_matrix_correlation(self) -> None:
        resolved = universes.resolve_universes(
            _correlated(count=20000), {"qelNorm": 1.0, "mecNorm": 1.0}
        )
        z = np.array([resolved["z"]["qelNorm"], resolved["z"]["mecNorm"]])
        np.testing.assert_allclose(np.corrcoef(z), RHO, atol=0.02)
        np.testing.assert_allclose(z.std(axis=1), 1.0, atol=0.02)
        values = np.array([resolved["values"]["qelNorm"], resolved["values"]["mecNorm"]])
        np.testing.assert_allclose(np.corrcoef(values), RHO, atol=0.02)

    def test_first_member_keeps_its_stream_and_others_are_untouched(self) -> None:
        # L is lower triangular with L[0, 0] = 1: the first member is unchanged.
        plain = universes.resolve_universes(
            _block(qelNorm={"sigma": 0.1}, mecNorm={"sigma": 0.1}, resNorm={"sigma": 0.1}),
            {"qelNorm": 1.0, "mecNorm": 1.0, "resNorm": 1.0},
        )
        grouped = universes.resolve_universes(
            _correlated(resNorm={"sigma": 0.1}),
            {"qelNorm": 1.0, "mecNorm": 1.0, "resNorm": 1.0},
        )
        self.assertEqual(grouped["values"]["resNorm"], plain["values"]["resNorm"])
        np.testing.assert_allclose(grouped["z"]["qelNorm"], plain["z"]["qelNorm"])
        self.assertNotEqual(grouped["z"]["mecNorm"], plain["z"]["mecNorm"])
        self.assertNotIn("correlations", plain)
        self.assertEqual(grouped["correlations"][0]["parameters"], ["qelNorm", "mecNorm"])
        self.assertEqual(grouped["correlations"][0]["matrix"], RHO)

    def test_log_parameters_correlate_in_log_space(self) -> None:
        block = _correlated(count=20000)
        for spec in block["parameters"].values():
            spec.update(sigma=0.5, log=True)
        resolved = universes.resolve_universes(block, {"qelNorm": 1.0, "mecNorm": 1.0})
        logs = np.log([resolved["values"]["qelNorm"], resolved["values"]["mecNorm"]])
        np.testing.assert_allclose(np.corrcoef(logs), RHO, atol=0.02)

    def _errors(self, block: dict) -> list[str]:
        return universes.validate_universes(block, "u", "nuwro")

    def test_valid_group(self) -> None:
        self.assertEqual(self._errors(_correlated()), [])

    def test_invalid_groups(self) -> None:
        not_pd = [[1, 0.9, -0.9], [0.9, 1, 0.9], [-0.9, 0.9, 1]]
        cases = {
            "non-empty list": dict(correlations=[]),
            "at least two": dict(correlations=[{"parameters": ["qelNorm"], "matrix": [[1]]}]),
            "not in the block": dict(
                correlations=[{"parameters": ["qelNorm", "resNorm"], "matrix": RHO}]
            ),
            "twice": dict(correlations=[{"parameters": ["qelNorm", "qelNorm"], "matrix": RHO}]),
            "another group": dict(correlations=[
                {"parameters": ["qelNorm", "mecNorm"], "matrix": RHO},
                {"parameters": ["mecNorm", "qelNorm"], "matrix": RHO},
            ]),
            "2x2": dict(correlations=[{"parameters": ["qelNorm", "mecNorm"], "matrix": [[1, 0.5]]}]),
            "diagonal must be 1": dict(
                correlations=[{"parameters": ["qelNorm", "mecNorm"], "matrix": [[2, 0], [0, 1]]}]
            ),
            "symmetric": dict(
                correlations=[{"parameters": ["qelNorm", "mecNorm"], "matrix": [[1, 0.5], [0.4, 1]]}]
            ),
            r"\[-1, 1\]": dict(
                correlations=[{"parameters": ["qelNorm", "mecNorm"], "matrix": [[1, 1.5], [1.5, 1]]}]
            ),
            "unknown keys": dict(
                correlations=[{"parameters": ["qelNorm", "mecNorm"], "matrix": RHO, "sigma": 1}]
            ),
        }
        for message, override in cases.items():
            with self.subTest(message):
                block = {**_correlated(), **override}
                self.assertRegex(" | ".join(self._errors(block)), message)
        with self.subTest("positive definite"):
            block = _correlated(matrix=not_pd, names=("qelNorm", "mecNorm", "resNorm"))
            self.assertRegex(" | ".join(self._errors(block)), "not positive definite")


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
        {"event_id": i, "seed": 1, "energy_gev": 1.0, "is_cc": True, "interaction": "qel",
         "probe": "numu", "target": "C12", "generator": "nuwro"}
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

    def test_last_bit_differences_in_values_are_merged(self) -> None:
        # exp of a log throw can differ by 1 ulp between CPUs; z is what must match.
        meta = {"seed": 7, "count": 2, "z": {"p": [0.1, -0.2]}, "values": {"p": [1.1, 0.9]}}
        nudged = {**meta, "values": {"p": [np.nextafter(1.1, 2.0), 0.9]}}
        with tempfile.TemporaryDirectory() as tmp:
            a = _chunk(Path(tmp) / "a.h5", 1, np.ones((1, 2)), meta)
            b = _chunk(Path(tmp) / "b.h5", 1, np.ones((1, 2)), nudged)
            merge_hdf5_files([a, b], Path(tmp) / "m.h5")
            c = _chunk(Path(tmp) / "c.h5", 1, np.ones((1, 2)), {**meta, "values": {"p": [1.2, 0.9]}})
            with self.assertRaisesRegex(MergeError, "drawn differently"):
                merge_hdf5_files([a, c], Path(tmp) / "m2.h5")

    def test_partial_universe_weights_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = _chunk(Path(tmp) / "a.h5", 1, np.ones((1, 2)), self.META)
            b = _chunk(Path(tmp) / "b.h5", 1, None, None)
            with self.assertRaisesRegex(MergeError, "carry universe_weights"):
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
        centrals = universes.nuwro_centrals(
            parameters, {"qel_axial_ff_set": 8, "qel_minerva_ff_scale": 0.0}
        )
        resolved = universes.resolve_universes(
            {"seed": 7, "count": 2, "parameters": parameters}, centrals
        )
        binary = any(name in universes.NUWRO_REWEIGHT_PARAMS for name in parameters)
        (tmp / "universes.json").write_text(
            json.dumps({"universes": resolved, "binary_universes": binary})
        )
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


GENIE_JOB = {"generator": "genie", "code_version": "R-3_06_00", "config_version": "G18_10a_02_11a"}


class GenieValidationTests(unittest.TestCase):
    def _config(self, **section) -> None:
        run_config([job(**GENIE_JOB, genie=section)])

    def test_valid_universes_and_variations(self) -> None:
        self._config(
            universes=_block(MaCCQE={"sigma": 0.15, "log": True}, MFP_pi={"sigma": 0.2}),
            variations={"RPA_CCQE": {"value": 1, "source": "x"}, "DecayAngMEC": None},
        )

    def test_broken_dial_says_why(self) -> None:
        with self.assertRaisesRegex(ConfigError, "not cross-section preserving"):
            self._config(universes=_block(FormZone={"sigma": 0.1}))

    def test_switch_is_not_a_gaussian_parameter(self) -> None:
        with self.assertRaisesRegex(ConfigError, "variations"):
            self._config(universes=_block(RPA_CCQE={"sigma": 0.5}))

    def test_gaussian_parameter_is_not_a_switch(self) -> None:
        with self.assertRaisesRegex(ConfigError, "universes"):
            self._config(variations={"MaCCQE": {}})

    def test_variation_value_range(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"\(0, 1\]"):
            self._config(variations={"RPA_CCQE": {"value": 1.5}})

    def test_exclusive_engine_modes(self) -> None:
        with self.assertRaisesRegex(ConfigError, "incompatible"):
            self._config(universes=_block(MaCCQE={"sigma": 0.1}, NormCCQE={"sigma": 0.1}))

    def test_one_fate_stays_the_cushion(self) -> None:
        fates = {f: {"sigma": 0.2} for f in ("FrCEx_pi", "FrInel_pi", "FrAbs_pi", "FrPiProd_pi")}
        with self.assertRaisesRegex(ConfigError, "cushion"):
            self._config(universes=_block(**fates))

    def test_nuwro_has_no_variations(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key 'nuwro.variations'"):
            run_config([job(**NUWRO_JOB, nuwro={"variations": {"RPA_CCQE": {}}})])

    def test_genie_section_only_on_genie_jobs(self) -> None:
        with self.assertRaisesRegex(ConfigError, "only valid on a genie job"):
            run_config([job(**NUWRO_JOB, genie={"universes": _block(MaCCQE={"sigma": 0.1})})])

    def _tune(self, tune: str, code_version: str = "R-3_06_00", **section) -> None:
        run_config(
            [job(**{**GENIE_JOB, "code_version": code_version, "config_version": tune}, genie=section)]
        )

    def test_fsi_dials_need_ha2018(self) -> None:
        with self.assertRaisesRegex(ConfigError, "MFP_pi' needs hA2018 FSI.*HNIntranuke2018"):
            self._tune("G18_10b_02_11a", universes=_block(MFP_pi={"sigma": 0.2}))

    def test_ccqe_dials_need_a_dipole_form_factor(self) -> None:
        for dial in ("MaCCQE", "NormCCQE"):
            with self.assertRaisesRegex(ConfigError, f"{dial}' needs a dipole.*ZExp"):
                self._tune("AR23_20i_00_000", universes=_block(**{dial: {"sigma": 0.1}}))

    def test_ccqe_dials_under_zexp_point_to_the_zexp_dials(self) -> None:
        with self.assertRaisesRegex(ConfigError, "MaCCQE' needs a dipole.*use ZNormCCQE"):
            self._tune("AR23_20i_00_000", universes=_block(MaCCQE={"sigma": 0.1}))

    def test_zexp_dials_need_a_zexp_form_factor(self) -> None:
        for tune, model in (
            ("G18_10a_02_11a", "NievesQELCCPXSec/Dipole"),
            ("G18_02a_00_000", "LwlynSmithQELCCPXSec/Dipole"),
            ("G21_11a_00_000", "HybridXSecAlgorithm/SuSAv2-QEL"),
        ):
            for dial in universes.GENIE_ZEXP_DIALS:
                with self.subTest(tune=tune, dial=dial):
                    with self.assertRaisesRegex(ConfigError, f"{dial}' needs a z-expansion.*{model}"):
                        self._tune(tune, universes=_block(**{dial: {"sigma": 0.1}}))

    def test_zexp_dials_on_their_validated_tune_do_not_warn(self) -> None:
        dials = {dial: {"sigma": 0.1, "log": True} for dial in universes.GENIE_ZEXP_DIALS}
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            self._tune("AR23_20i_00_000", universes=_block(**dials, MFP_pi={"sigma": 0.2}))
        self.assertFalse([line for line in logs.output if "unvalidated" in line])

    def _zexp_warnings(self, **block_extra) -> list[str]:
        dials = {dial: {"sigma": 0.5} for dial in universes.GENIE_ZEXP_COEFF_DIALS}
        block = {**_block(**dials), **block_extra}
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            self._tune("AR23_20i_00_000", universes=block, variations={"RPA_CCQE": None})
        return [line for line in logs.output if "without a shared correlation group" in line]

    def test_independent_zexp_coefficients_warn(self) -> None:
        self.assertEqual(len(self._zexp_warnings()), 1)

    def test_correlated_zexp_coefficients_do_not_warn(self) -> None:
        group = {"parameters": list(universes.GENIE_ZEXP_COEFF_DIALS), "matrix": np.eye(4).tolist()}
        self.assertEqual(self._zexp_warnings(correlations=[group]), [])

    def test_zexp_coefficients_may_throw_negative_scales(self) -> None:
        values = universes.universe_values("ZExpA2CCQE", 1.0, 1.667, False, np.array([-2.0]))
        self.assertLess(values[0], 0)
        with self.assertRaisesRegex(ValueError, "ZNormCCQE"):
            universes.universe_values("ZNormCCQE", 1.0, 1.667, False, np.array([-2.0]))

    def test_coulomb_needs_the_nieves_model(self) -> None:
        for tune in ("G18_10a_02_11a", "AR23_20i_00_000"):
            self._tune(tune, universes=_block(CoulombCCQE={"sigma": 0.2, "log": True}))
        with self.assertRaisesRegex(ConfigError, "CoulombCCQE' needs the Nieves.*LwlynSmith"):
            self._tune("G18_02a_00_000", universes=_block(CoulombCCQE={"sigma": 0.2}))

    def _inert_warnings(self, jobs: list[dict]) -> list[str]:
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            run_config(jobs)
        return [line for line in logs.output if "cannot act on" in line]

    def _nonresbg_job(self, particle: str, current: str, **parameters) -> dict:
        return job(
            **GENIE_JOB,
            flux={"type": "power_law", "particle": particle, "emin_gev": 0.5, "emax_gev": 10.0,
                  "gamma": -2.0},
            physics={"mode": "inclusive", "current": current},
            genie={"universes": _block(MFP_pi={"sigma": 0.2}, **parameters)},
        )

    def test_nonresbg_dial_of_the_other_probe_or_current_warns(self) -> None:
        for particle, current, dial in (
            ("numu", "inclusive", "NonRESBGvbarpCC1pi"),
            ("numubar", "inclusive", "NonRESBGvpCC1pi"),
            ("numu", "cc", "NonRESBGvpNC1pi"),
            ("numubar", "nc", "NonRESBGvbarnCC2pi"),
        ):
            with self.subTest(particle=particle, current=current, dial=dial):
                warnings = self._inert_warnings(
                    [self._nonresbg_job(particle, current, **{dial: {"sigma": 0.5}})]
                )
                self.assertEqual(len(warnings), 1)
                self.assertIn(dial, warnings[0])

    def test_nonresbg_dial_of_the_job_probe_and_current_does_not_warn(self) -> None:
        for particle, current, dial in (
            ("numu", "inclusive", "NonRESBGvpNC2pi"),
            ("numubar", "cc", "NonRESBGvbarpCC1pi"),
            ("nuebar", "nc", "NonRESBGvbarnNC1pi"),
        ):
            with self.subTest(particle=particle, current=current, dial=dial):
                warnings = self._inert_warnings(
                    [self._nonresbg_job(particle, current, **{dial: {"sigma": 0.5}})]
                )
                self.assertEqual(warnings, [])

    def test_inert_warning_is_logged_once_per_flavor(self) -> None:
        dials = {"NonRESBGvpCC1pi": {"sigma": 0.5}, "NonRESBGvbarpCC1pi": {"sigma": 0.5}}
        jobs = [
            {**self._nonresbg_job(particle, "cc", **dials), "target": {"nucleus": nucleus}}
            for particle in ("numu", "numubar")
            for nucleus in ("C12", "Ar40")
        ]
        warnings = self._inert_warnings(jobs)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("NonRESBGvbarpCC1pi cannot act on neutrino" in w for w in warnings))
        self.assertTrue(any("NonRESBGvpCC1pi cannot act on antineutrino" in w for w in warnings))

    def test_rpa_needs_the_nieves_model(self) -> None:
        with self.assertRaisesRegex(ConfigError, "RPA_CCQE' needs the Nieves.*LwlynSmith"):
            self._tune("G18_02a_00_000", variations={"RPA_CCQE": {}})

    def test_mec_shape_needs_a_non_empirical_tune(self) -> None:
        with self.assertRaisesRegex(ConfigError, "already uses"):
            self._tune("G18_02a_00_000", variations={"XSecShape_CCMEC": {}})

    def test_code_version_without_reweighter(self) -> None:
        with self.assertRaisesRegex(ConfigError, "built and checked only for R-3_06_00"):
            self._tune("G18_10a_02_11a", "R-3_04_00", universes=_block(MaCCQE={"sigma": 0.1}))

    def test_unknown_model_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"'GEM21_11a'\) is not in the reweighting model table"):
            self._tune("GEM21_11a_00_000", universes=_block(NormCCMEC={"sigma": 0.5}))

    def test_dial_under_an_unchecked_model_warns_once(self) -> None:
        section = {"universes": _block(NormCCMEC={"sigma": 0.5}, MFP_pi={"sigma": 0.2})}
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            run_config(
                [
                    job(**{**GENIE_JOB, "config_version": "N24_20i_02_11b"}, genie=section,
                        target={"nucleus": nucleus})
                    for nucleus in ("C12", "O16", "Ar40")
                ]
            )
        warnings = [line for line in logs.output if "unvalidated" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("NormCCMEC", warnings[0])
        self.assertIn("SuSAv2MECPXSec", warnings[0])

    def test_validated_tune_does_not_warn(self) -> None:
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            self._tune(
                "G18_10a_02_11b",
                universes=_block(MaCCQE={"sigma": 0.1}, MFP_pi={"sigma": 0.2}),
                variations={"RPA_CCQE": {}, "XSecShape_CCMEC": {}},
            )
        self.assertFalse([line for line in logs.output if "unvalidated" in line])

    def test_model_table_matches_the_payload(self) -> None:
        import xml.etree.ElementTree as ET

        for code_version, by_cmc in universes.GENIE_TUNE_MODELS.items():
            config_dir = Path(f"/opt/nf/generators/genie/{code_version}/genie/config")
            if not config_dir.is_dir():
                self.skipTest(f"no GENIE {code_version} payload here")
            keys = {
                "HadronTransp-Model": "fsi",
                **{
                    f"XSecModel@genie::EventGenerator/{process}": field
                    for process, field in (
                        ("QEL-CC", "qel_cc"), ("RES-CC", "res_cc"), ("COH-CC-PION", "coh_cc_pion"),
                        ("DIS-CC", "dis_cc"), ("MEC-CC", "mec_cc"),
                    )
                },
            }
            found = {}
            for path in sorted(config_dir.glob("*/ModelConfiguration.xml")):
                models = {}
                for p in ET.parse(path).getroot().iter("param"):
                    name = p.get("name")
                    if name is None or name not in keys:
                        continue
                    models[keys[name]] = (p.text or "").strip().removeprefix("genie::")
                if len(models) == len(keys):
                    found[path.parent.name] = universes.GenieModels(**models)
            self.assertEqual(found, by_cmc)

    def test_driver_table_matches_the_allowlist(self) -> None:
        # nf_genie_reweight must accept exactly the dials the config accepts.
        source = (Path(__file__).parent.parent / "setup/genie/nf_genie_reweight.cc").read_text()
        table = source[source.index("table = {"):source.index("};", source.index("table = {"))]
        import re

        driver = set(re.findall(r'\{"(\w+)", "\w+"\}', table))
        self.assertEqual(
            driver, set(universes.GENIE_SCALE_DIALS) | set(universes.GENIE_SWITCH_DIALS)
        )


class GenieDialCatalogTests(unittest.TestCase):
    """configs/schema/genie_dials.yaml lists every accepted dial at GENIE's 1 sigma."""

    CATALOG = Path(__file__).parent.parent / "configs/schema/genie_dials.yaml"

    def _entries(self) -> dict[str, float]:
        # Commented-out entries (the fate cushions) count as listed.
        import re

        found = re.findall(
            r"^\s*#?\s*(\w+): \{sigma: ([\d.]+)", self.CATALOG.read_text(), re.MULTILINE
        )
        return {name: float(sigma) for name, sigma in found}

    def test_lists_exactly_the_allowlist(self) -> None:
        import yaml

        self.assertEqual(set(self._entries()), set(universes.GENIE_SCALE_DIALS))
        block = yaml.safe_load(self.CATALOG.read_text())["genie"]
        self.assertEqual(set(block["variations"]), set(universes.GENIE_SWITCH_DIALS))

    def test_sigmas_are_genie_defaults(self) -> None:
        import xml.etree.ElementTree as ET

        for path in (
            Path("/opt/nf/generators/genie/R-3_06_00/genie/config/GSystUncertaintyTable.xml"),
            Path(__file__).parent.parent / "scratch/GSystUncertaintyTable.xml",
        ):
            if path.is_file():
                break
        else:
            self.skipTest("no GSystUncertaintyTable.xml here")
        table = {
            p.get("name"): float(p.text or "nan")
            for p in ET.parse(path).getroot().iter("param")
            if p.get("type") == "double"
        }
        for name, sigma in self._entries().items():
            if name in universes.GENIE_ZEXP_COEFF_DIALS:
                continue  # Meyer et al.'s, test_zexp_follows_meyer_2016
            larger = max(table[f"{name}@PlusOneSigma"], table[f"{name}@MinusOneSigma"])
            self.assertAlmostEqual(sigma, larger, msg=name)

    def test_zexp_follows_meyer_2016(self) -> None:
        # Meyer et al. 2016, PRD 93, 113015: Eq. 36 (a, delta_a) and Eq. 38 (C).
        a = np.array([2.30, -0.6, -3.8, 2.3])
        delta_a = np.array([0.13, 1.0, 2.5, 2.7])
        corr = np.array([
            [1, 0.350, -0.678, 0.611],
            [0.350, 1, -0.898, 0.367],
            [-0.678, -0.898, 1, -0.685],
            [0.611, 0.367, -0.685, 1],
        ])
        import yaml

        block = yaml.safe_load(self.CATALOG.read_text())["genie"]["universes"]
        names = list(universes.GENIE_ZEXP_COEFF_DIALS)
        for name, expected in zip(names, delta_a / np.abs(a)):
            spec = block["parameters"][name]
            self.assertAlmostEqual(spec["sigma"], expected, delta=5e-4, msg=name)
            self.assertFalse(spec.get("log", False), msg=name)
        (group,) = [g for g in block["correlations"] if set(g["parameters"]) & set(names)]
        self.assertEqual(group["parameters"], names)
        np.testing.assert_array_equal(group["matrix"], corr * np.outer(np.sign(a), np.sign(a)))
        # The whole catalog holds exclusive dials on purpose; check this part only.
        zexp = {"seed": 1, "count": 1, "parameters": {n: block["parameters"][n] for n in names},
                "correlations": [group]}
        self.assertEqual(universes.validate_universes(zexp, "catalog", "genie"), [])

    def test_zexp_centrals_match_the_payload(self) -> None:
        path = Path(
            "/opt/nf/generators/genie/R-3_06_00/genie/config/ZExpAxialFormFactorModel.xml"
        )
        if not path.is_file():
            self.skipTest("no GENIE payload here")
        import xml.etree.ElementTree as ET

        params = {p.get("name"): p.text for p in ET.parse(path).getroot().iter("param")}
        payload = [float(params[f"QEL-Z_A{i}"] or "nan") for i in range(1, 5)]
        self.assertEqual(payload, [2.30, -0.6, -3.8, 2.3])


class LoadWeightsTests(unittest.TestCase):
    def _write(self, tmp: Path, weights: np.ndarray, count: int, switches: list[str]) -> dict:
        block = {"seed": 1, "count": count, "parameters": {"MaCCQE": {"sigma": 0.1, "log": True}}}
        resolved = {
            "universes": universes.resolve_universes(block, {"MaCCQE": 1.0}),
            "variations": universes.resolve_variations(dict.fromkeys(switches), "genie")
            if switches
            else None,
            "binary_universes": True,
        }
        import uproot

        with uproot.recreate(tmp / universes.WEIGHTS_FILE) as f:
            f["weights"] = {"weights": weights}
        return resolved

    def test_universe_then_variation_columns(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            w = np.array([[1.1, 0.9, 1.3], [1.0, 1.0, 0.5]])
            resolved = self._write(tmp, w, 2, ["RPA_CCQE"])
            uw, vw = universes.load_weights(tmp, resolved, 2)
            assert uw is not None and vw is not None
            np.testing.assert_allclose(uw, w[:, :2], rtol=1e-6)
            np.testing.assert_allclose(vw, w[:, 2:], rtol=1e-6)
            self.assertEqual(vw.dtype, np.float32)

    def test_inert_variation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            resolved = self._write(tmp, np.array([[1.1, 1.0], [0.9, 1.0]]), 1, ["RPA_CCQE"])
            with self.assertRaisesRegex(RuntimeError, "RPA_CCQE"):
                universes.load_weights(tmp, resolved, 2)

    def test_variation_metadata(self) -> None:
        meta = universes.resolve_variations({"RPA_CCQE": {"value": 0.5}, "DecayAngMEC": None}, "genie")
        self.assertEqual(meta["columns"], ["RPA_CCQE", "DecayAngMEC"])
        self.assertEqual(meta["parameters"]["RPA_CCQE"]["value"], 0.5)
        self.assertEqual(meta["parameters"]["DecayAngMEC"]["value"], 1.0)
        self.assertIn("RPA off", meta["parameters"]["RPA_CCQE"]["meaning"])


class VariationMergeTests(unittest.TestCase):
    def test_variation_weights_are_concatenated(self) -> None:
        meta = {"columns": ["RPA_CCQE"]}
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, rows in enumerate(([[1.5], [0.5]], [[2.0]])):
                metadata = {
                    "generator": "genie", "code_version": "R-3_06_00",
                    "config_version": "G18_10a_02_11a", "run_name": "t", "chunk_id": i,
                    "seed": 1, "xsec_norm_count": float(len(rows)), "variations": meta,
                }
                events = [
                    {"event_id": j, "seed": 1, "energy_gev": 1.0, "is_cc": True, "interaction": "qel",
                     "probe": "numu", "target": "C12", "generator": "genie"}
                    for j in range(len(rows))
                ]
                paths.append(write_common_hdf5(
                    Path(tmp) / f"{i}.h5", metadata, events, variation_weights=np.array(rows)
                ))
            out = merge_hdf5_files(paths, Path(tmp) / "m.h5")
            with h5py.File(out, "r") as f:
                np.testing.assert_array_equal(f["events/variation_weights"][()], [[1.5], [0.5], [2.0]])
                self.assertNotIn("universe_weights", f["events"])
                self.assertEqual(json.loads(f["metadata"].attrs["variations"]), meta)


class GenieSpecTests(unittest.TestCase):
    def test_rows_hold_universes_then_one_row_per_variation(self) -> None:
        from unittest import mock

        from neutrino_factory.generators.genie import GenieAdapter

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "translated_config.json").write_text(json.dumps({
                "config_version": "G18_10a_02_11a",
                "universes": {"seed": 3, "count": 2, "parameters": {"MaCCQE": {"sigma": 0.1}}},
                "variations": {"RPA_CCQE": {"value": 0.5}, "DecayAngMEC": None},
            }))
            adapter = GenieAdapter.__new__(GenieAdapter)
            with mock.patch.object(GenieAdapter, "_run_reweight_binary") as run:
                adapter._run_reweight(tmp, "R-3_06_00")
            run.assert_called_once_with(tmp, "R-3_06_00", "G18_10a_02_11a")
            lines = (tmp / universes.SPEC_FILE).read_text().splitlines()
            self.assertEqual(lines[0], "MaCCQE:scale RPA_CCQE:raw DecayAngMEC:raw")
            ma = 1.0 + 0.1 * universes.draw_z(3, "MaCCQE", 2)
            rows = [list(map(float, line.split())) for line in lines[1:]]
            np.testing.assert_allclose(rows, [[ma[0], 0, 0], [ma[1], 0, 0], [1, 0.5, 0], [1, 0, 1]])
            resolved = json.loads((tmp / universes.RESOLVED_FILE).read_text())
            self.assertEqual(resolved["universes"]["parameters"]["MaCCQE"]["central"], 1.0)

    def test_binary_runs_in_batches_joined_in_spec_order(self) -> None:
        from unittest import mock

        import uproot

        from neutrino_factory.generators import genie
        from neutrino_factory.generators.genie import GenieAdapter

        n_events, n_rows = 4, 7

        def fake_binary(work_dir, code_version, tune, spec, out):
            # Weight of (event i, spec row with first value v) is v + i / 10.
            first = [float(line.split()[0]) for line in (work_dir / spec).read_text().splitlines()[1:]]
            weights = np.array(first)[None, :] + np.arange(n_events)[:, None] / 10
            with uproot.recreate(work_dir / out) as f:
                f["weights"] = {"weights": weights}

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            universes.write_spec(tmp / universes.SPEC_FILE, ["MaCCQE:scale"], [[k] for k in range(n_rows)])
            adapter = GenieAdapter.__new__(GenieAdapter)
            with mock.patch.object(genie, "REWEIGHT_BATCH_ROWS", 3), mock.patch.object(
                GenieAdapter, "_run_reweight_batch", side_effect=fake_binary
            ) as run:
                adapter._run_reweight_binary(tmp, "R-3_06_00", "AR23_20i_00_000")
            self.assertEqual(run.call_count, 3)
            with uproot.open(tmp / universes.WEIGHTS_FILE) as f:
                weights = f["weights"]["weights"].array(library="np")
            expected = np.arange(n_rows)[None, :] + np.arange(n_events)[:, None] / 10
            np.testing.assert_array_equal(weights, expected)
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), [universes.WEIGHTS_FILE, universes.SPEC_FILE])

    def test_small_spec_runs_the_binary_once_on_the_spec_itself(self) -> None:
        from unittest import mock

        from neutrino_factory.generators.genie import GenieAdapter

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            universes.write_spec(tmp / universes.SPEC_FILE, ["MaCCQE:scale"], [[1.0], [1.1]])
            adapter = GenieAdapter.__new__(GenieAdapter)
            with mock.patch.object(GenieAdapter, "_run_reweight_batch") as run:
                adapter._run_reweight_binary(tmp, "R-3_06_00", "AR23_20i_00_000")
            run.assert_called_once_with(
                tmp, "R-3_06_00", "AR23_20i_00_000", universes.SPEC_FILE, universes.WEIGHTS_FILE
            )


if __name__ == "__main__":
    unittest.main()
