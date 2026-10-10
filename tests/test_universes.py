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

    def test_driver_table_matches_the_allowlist(self) -> None:
        # nf_genie_reweight must accept exactly the dials the config accepts.
        source = (Path(__file__).parent.parent / "setup/genie/nf_genie_reweight.cc").read_text()
        table = source[source.index("table = {"):source.index("};", source.index("table = {"))]
        import re

        driver = set(re.findall(r'\{"(\w+)", "\w+"\}', table))
        self.assertEqual(
            driver, set(universes.GENIE_SCALE_DIALS) | set(universes.GENIE_SWITCH_DIALS)
        )


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
                    {"event_id": j, "seed": 1, "energy_gev": 1.0, "is_cc": True, "interaction": "qel"}
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


if __name__ == "__main__":
    unittest.main()
