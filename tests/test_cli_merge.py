from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.cli import build_parser


class MergeCliTests(unittest.TestCase):
    def test_explicit_mode_still_merges_explicit_inputs(self) -> None:
        args = build_parser().parse_args(
            ["merge", "--output", "output/merged.h5", "output/chunk_000.h5", "output/chunk_001.h5"]
        )

        with patch("neutrino_factory.cli.merge_outputs", return_value="output/merged.h5") as merge_outputs:
            with patch("neutrino_factory.cli._print_json") as print_json:
                self.assertEqual(args.func(args), 0)

        merge_outputs.assert_called_once_with(
            ["output/chunk_000.h5", "output/chunk_001.h5"],
            "output/merged.h5",
        )
        print_json.assert_called_once_with({"merged_output": "output/merged.h5", "input_count": 2})

    def test_config_mode_rejects_output_argument(self) -> None:
        args = build_parser().parse_args(
            ["merge", "--config", "configs/examples/power_law_numu_Ar.yaml", "--output", "x.h5"]
        )
        with self.assertRaises(RuntimeError):
            args.func(args)

    def test_config_mode_rejects_positional_inputs(self) -> None:
        args = build_parser().parse_args(
            ["merge", "--config", "configs/examples/power_law_numu_Ar.yaml", "output/chunk_000.h5"]
        )
        with self.assertRaises(RuntimeError):
            args.func(args)

    def test_explicit_mode_requires_output(self) -> None:
        args = build_parser().parse_args(["merge", "output/chunk_000.h5"])
        with self.assertRaises(RuntimeError):
            args.func(args)

    def test_config_mode_merges_per_target_and_skips_invalid_chunks(self) -> None:
        args = build_parser().parse_args(
            ["merge", "--config", "configs/examples/power_law_numu_Ar.yaml"]
        )

        outputs = {
            "chunks": [
                {
                    "path": Path("output/chunks/genie/v1/chunk_000.h5"),
                    "generator": "genie",
                    "version_id": "v1",
                },
                {
                    "path": Path("output/chunks/genie/v1/chunk_001.h5"),
                    "generator": "genie",
                    "version_id": "v1",
                },
                {
                    "path": Path("output/chunks/nuwro/v2/chunk_000.h5"),
                    "generator": "nuwro",
                    "version_id": "v2",
                },
            ],
            "merged": [
                {
                    "path": Path("output/merged/run_genie_v1.h5"),
                    "generator": "genie",
                    "version_id": "v1",
                },
                {
                    "path": Path("output/merged/run_nuwro_v2.h5"),
                    "generator": "nuwro",
                    "version_id": "v2",
                },
            ],
        }

        validation = {
            "output/chunks/genie/v1/chunk_000.h5": {
                "valid": True,
                "errors": [],
            },
            "output/chunks/genie/v1/chunk_001.h5": {
                "valid": False,
                "errors": ["File does not exist"],
            },
            "output/chunks/nuwro/v2/chunk_000.h5": {
                "valid": False,
                "errors": ["Missing required metadata: expected_events"],
            },
        }

        validate_calls: list[tuple[str, object]] = []

        def _validate(path: str | Path, expected_events: object = 123, tolerance: float = 0.05) -> dict:
            del tolerance
            validate_calls.append((str(path), expected_events))
            return validation[str(path)]

        stdout = io.StringIO()
        with patch("neutrino_factory.cli.load_config", return_value={"run": {"name": "run"}}):
            with patch("neutrino_factory.cli.expected_outputs", return_value=outputs):
                with patch("neutrino_factory.cli.validate_file", side_effect=_validate):
                    with patch(
                        "neutrino_factory.cli.merge_outputs",
                        side_effect=lambda inputs, output: str(output),
                    ) as merge_outputs:
                        with patch("neutrino_factory.cli._print_json") as print_json:
                            with redirect_stdout(stdout):
                                self.assertEqual(args.func(args), 0)

        merge_outputs.assert_called_once_with(
            [Path("output/chunks/genie/v1/chunk_000.h5")],
            Path("output/merged/run_genie_v1.h5"),
        )
        self.assertEqual(
            validate_calls,
            [
                ("output/chunks/genie/v1/chunk_000.h5", None),
                ("output/chunks/genie/v1/chunk_001.h5", None),
                ("output/chunks/nuwro/v2/chunk_000.h5", None),
            ],
        )

        payload = print_json.call_args.args[0]
        self.assertEqual(payload["merged_outputs"], ["output/merged/run_genie_v1.h5"])
        self.assertEqual(payload["merged_target_count"], 1)
        self.assertEqual(payload["skipped_target_count"], 1)
        self.assertEqual(len(payload["targets"]), 2)
        self.assertEqual(payload["targets"][0]["status"], "merged")
        self.assertEqual(payload["targets"][1]["status"], "skipped")

        warnings = stdout.getvalue()
        self.assertIn("Warning: skipping chunk output/chunks/genie/v1/chunk_001.h5", warnings)
        self.assertIn("Warning: skipping chunk output/chunks/nuwro/v2/chunk_000.h5", warnings)
        self.assertIn("Warning: no valid chunk files available", warnings)


if __name__ == "__main__":
    unittest.main()