from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from neutrino_factory.jobs import (
    SEED_MODULUS,
    JobExpansionError,
    expand_jobs,
    job_label,
    job_seed,
    matrix_combinations,
    substitute,
)
from neutrino_factory.slurm import build_task_manifest

from .helpers import job, run_config

TRIPLE = {
    "generator": "genie",
    "code_version": "R-3_06_00",
    "config_version": "G18_10a_02_11a",
}


def _macro(**body_overrides) -> dict:
    """A macro whose job body is parameterized by generator, flavour and nucleus."""
    body = {
        "generator": "{{generator}}",
        "code_version": "{{code_version}}",
        "config_version": "{{config_version}}",
        "flux": {
            "type": "power_law",
            "particle": "{{particle}}",
            "emin_gev": 0.5,
            "emax_gev": 10.0,
            "gamma": -2.0,
        },
        "target": {"nucleus": "{{nucleus}}"},
    }
    body.update(body_overrides)
    return {
        "scan": {
            "params": {
                "generator": None,
                "code_version": None,
                "config_version": None,
                "particle": "numu",
                "nucleus": "C12",
            },
            "job": body,
        }
    }


class SubstitutionTests(unittest.TestCase):
    def test_whole_value_placeholder_preserves_type(self) -> None:
        # The load-bearing rule: a macro must be able to template numeric fields.
        result = substitute({"events": "{{n}}"}, {"n": 100000}, "where")
        self.assertIsInstance(result["events"], int)
        self.assertEqual(result["events"], 100000)

    def test_whole_value_placeholder_preserves_other_types(self) -> None:
        params = {"f": 1.5, "b": True, "l": [1, 2], "d": {"k": "v"}}
        result = substitute({k: "{{%s}}" % k for k in params}, params, "where")
        self.assertIsInstance(result["f"], float)
        self.assertIsInstance(result["b"], bool)
        self.assertEqual(result["l"], [1, 2])
        self.assertEqual(result["d"], {"k": "v"})

    def test_interpolated_placeholder_yields_a_string(self) -> None:
        result = substitute("scan_{{particle}}_{{nucleus}}", {"particle": "numu", "nucleus": "C12"}, "w")
        self.assertEqual(result, "scan_numu_C12")

    def test_substitutes_inside_nested_structures(self) -> None:
        node = {"a": {"b": ["{{x}}", "pre_{{x}}"]}}
        self.assertEqual(substitute(node, {"x": "v"}, "w"), {"a": {"b": ["v", "pre_v"]}})

    def test_unknown_placeholder_names_itself_and_the_declared_params(self) -> None:
        with self.assertRaises(JobExpansionError) as ctx:
            substitute("{{typo}}", {"particle": "numu"}, "jobs[2]")
        message = str(ctx.exception)
        self.assertIn("jobs[2]", message)
        self.assertIn("typo", message)
        self.assertIn("particle", message)

    def test_env_syntax_in_a_macro_body_still_expands(self) -> None:
        # ${VAR:-default} is config.py's, {{param}} is this module's; they must
        # not interfere.
        with patch.dict(os.environ, {"NF_OUTPUT_ROOT": "/tmp/nf-env-test"}, clear=False):
            config = run_config(
                [job()], storage={"output_root": "${NF_OUTPUT_ROOT:-./output}"}
            )
        self.assertEqual(config["storage"]["output_root"], "/tmp/nf-env-test")


class ParameterTests(unittest.TestCase):
    def test_missing_required_parameter_raises(self) -> None:
        with self.assertRaises(JobExpansionError) as ctx:
            expand_jobs({"macros": _macro(), "jobs": [{"use": "scan"}]})
        self.assertIn("generator", str(ctx.exception))

    def test_undeclared_parameter_raises(self) -> None:
        with self.assertRaises(JobExpansionError) as ctx:
            expand_jobs(
                {"macros": _macro(), "jobs": [{"use": "scan", "with": {**TRIPLE, "nuclues": "C12"}}]}
            )
        self.assertIn("nuclues", str(ctx.exception))

    def test_unknown_macro_lists_the_known_ones(self) -> None:
        with self.assertRaises(JobExpansionError) as ctx:
            expand_jobs({"macros": _macro(), "jobs": [{"use": "scam"}]})
        self.assertIn("scan", str(ctx.exception))

    def test_precedence_is_defaults_then_with_then_matrix(self) -> None:
        jobs = expand_jobs(
            {
                "macros": _macro(),
                "jobs": [
                    {
                        "use": "scan",
                        "with": {**TRIPLE, "nucleus": "O16"},
                        "matrix": {"nucleus": ["Ar40"]},
                    }
                ],
            }
        )
        # default C12 < with O16 < matrix Ar40
        self.assertEqual(jobs[0]["target"]["nucleus"], "Ar40")

    def test_inline_keys_win_over_the_macro_body(self) -> None:
        jobs = expand_jobs(
            {
                "macros": _macro(),
                "jobs": [{"use": "scan", "with": TRIPLE, "flux": {"gamma": -1.0}}],
            }
        )
        self.assertEqual(jobs[0]["flux"]["gamma"], -1.0)
        self.assertEqual(jobs[0]["flux"]["emax_gev"], 10.0)

    def test_job_defaults_fill_what_is_left_out(self) -> None:
        jobs = expand_jobs({"macros": _macro(), "jobs": [{"use": "scan", "with": TRIPLE}]})
        self.assertEqual(jobs[0]["chunks"], 1)
        self.assertEqual(jobs[0]["physics"]["mode"], "inclusive")


class MatrixTests(unittest.TestCase):
    def test_axes_are_crossed_in_product_order(self) -> None:
        jobs = expand_jobs(
            {
                "macros": _macro(),
                "jobs": [
                    {
                        "use": "scan",
                        "with": TRIPLE,
                        "matrix": {
                            "particle": ["numu", "numubar"],
                            "nucleus": ["C12", "O16", "Ar40"],
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            [(j["flux"]["particle"], j["target"]["nucleus"]) for j in jobs],
            [
                ("numu", "C12"), ("numu", "O16"), ("numu", "Ar40"),
                ("numubar", "C12"), ("numubar", "O16"), ("numubar", "Ar40"),
            ],
        )

    def test_include_is_a_coupled_axis(self) -> None:
        jobs = expand_jobs(
            {
                "macros": _macro(),
                "jobs": [
                    {
                        "use": "scan",
                        "matrix": {
                            "particle": ["numu", "numubar"],
                            "include": [
                                TRIPLE,
                                {"generator": "nuwro", "code_version": "nuwro_25.11",
                                 "config_version": "default"},
                            ],
                        },
                    }
                ],
            }
        )
        self.assertEqual(len(jobs), 4)
        self.assertEqual(
            [(j["generator"], j["config_version"], j["flux"]["particle"]) for j in jobs],
            [
                ("genie", "G18_10a_02_11a", "numu"),
                ("nuwro", "default", "numu"),
                ("genie", "G18_10a_02_11a", "numubar"),
                ("nuwro", "default", "numubar"),
            ],
        )

    def test_exclude_matches_on_a_partial_key_set(self) -> None:
        jobs = expand_jobs(
            {
                "macros": _macro(),
                "jobs": [
                    {
                        "use": "scan",
                        "with": TRIPLE,
                        "matrix": {
                            "particle": ["numu", "numubar"],
                            "nucleus": ["C12", "Ar40"],
                            "exclude": [{"nucleus": "Ar40"}],
                        },
                    }
                ],
            }
        )
        self.assertEqual([j["target"]["nucleus"] for j in jobs], ["C12", "C12"])

    def test_exclude_on_an_unknown_key_raises(self) -> None:
        # A silent no-op would generate jobs the user believes are excluded.
        with self.assertRaises(JobExpansionError) as ctx:
            matrix_combinations({"nucleus": ["C12"], "exclude": [{"nuclues": "C12"}]}, "jobs[0]")
        self.assertIn("nuclues", str(ctx.exception))

    def test_excluding_everything_raises(self) -> None:
        with self.assertRaises(JobExpansionError) as ctx:
            matrix_combinations({"nucleus": ["C12"], "exclude": [{"nucleus": "C12"}]}, "jobs[0]")
        self.assertIn("zero jobs", str(ctx.exception))

    def test_empty_axis_raises(self) -> None:
        with self.assertRaises(JobExpansionError):
            matrix_combinations({"nucleus": []}, "jobs[0]")

    def test_matrix_without_a_macro_uses_the_entry_as_the_body(self) -> None:
        jobs = expand_jobs(
            {
                "jobs": [
                    {
                        **TRIPLE,
                        "target": {"nucleus": "{{nucleus}}"},
                        "matrix": {"nucleus": ["C12", "O16"]},
                    }
                ]
            }
        )
        self.assertEqual([j["target"]["nucleus"] for j in jobs], ["C12", "O16"])


class LabelTests(unittest.TestCase):
    def test_label_encodes_the_whole_initial_state(self) -> None:
        # Pins the tokenization of the "+" in the version identifier.
        self.assertEqual(
            job_label(job(target={"nucleus": "C12"})),
            "genie_R-3_06_00_G18_10a_02_11a_numu_C12_cc",
        )

    def test_explicit_name_becomes_the_label(self) -> None:
        self.assertEqual(job_label(job(name="my scan/1")), "my_scan_1")

    def test_duplicate_labels_raise_and_name_both_entries(self) -> None:
        # These differ only in the energy range, which the label does not show.
        with self.assertRaises(JobExpansionError) as ctx:
            expand_jobs(
                {
                    "jobs": [
                        job(flux={"type": "power_law", "particle": "numu",
                                  "emin_gev": 0.5, "emax_gev": 10.0, "gamma": -2.0}),
                        job(flux={"type": "power_law", "particle": "numu",
                                  "emin_gev": 0.5, "emax_gev": 50.0, "gamma": -2.0}),
                    ]
                }
            )
        message = str(ctx.exception)
        self.assertIn("Duplicate job label", message)
        self.assertIn("0", message)
        self.assertIn("1", message)
        self.assertIn("name:", message)

    def test_a_label_does_not_depend_on_its_neighbours(self) -> None:
        # Labels go into output paths: adding a job must never rename another
        # job's files.
        alone = expand_jobs({"jobs": [job(target={"nucleus": "C12"})]})
        with_neighbour = expand_jobs(
            {"jobs": [job(generator="nuwro", code_version="nuwro_25.11",
                          config_version="default"),
                      job(target={"nucleus": "C12"})]}
        )
        self.assertEqual(alone[0]["label"], with_neighbour[1]["label"])

    def test_target_pdg_is_derived_during_expansion(self) -> None:
        jobs = expand_jobs({"jobs": [job(target={"nucleus": "Ar40"})]})
        self.assertEqual(jobs[0]["target"]["pdg"], 1000180400)


class SeedTests(unittest.TestCase):
    def test_seed_is_deterministic(self) -> None:
        self.assertEqual(job_seed(12345, "label", 3), job_seed(12345, "label", 3))

    def test_seeds_stay_in_the_int32_safe_range(self) -> None:
        for chunk_id in range(50):
            seed = job_seed(12345, "some_label", chunk_id)
            self.assertGreaterEqual(seed, 1)
            self.assertLessEqual(seed, SEED_MODULUS)

    def test_manifest_seeds_are_all_distinct(self) -> None:
        jobs = [
            job(generator="genie", config_version=tune, events=100, chunks=20)
            for tune in ("G18_10a_02_11a", "AR23_20i_00_000")
        ] + [job(generator="nuwro", code_version="nuwro_25.11",
                 config_version="default", events=100, chunks=20)]
        manifest = build_task_manifest(run_config(jobs))
        seeds = [task["seed"] for task in manifest["tasks"]]
        self.assertEqual(len(seeds), 60)
        self.assertEqual(len(set(seeds)), 60)

    def test_inserting_a_job_leaves_other_seeds_untouched(self) -> None:
        original = job(target={"nucleus": "C12"}, chunks=4)
        inserted = job(generator="nuwro", code_version="nuwro_25.11",
                       config_version="default", chunks=4)

        before = build_task_manifest(run_config([original]))
        after = build_task_manifest(run_config([inserted, original]))

        original_label = before["tasks"][0]["job_label"]
        self.assertEqual(
            [t["seed"] for t in before["tasks"]],
            [t["seed"] for t in after["tasks"] if t["job_label"] == original_label],
        )


if __name__ == "__main__":
    unittest.main()
