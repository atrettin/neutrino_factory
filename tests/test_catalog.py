from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from neutrino_factory import catalog
from neutrino_factory.cli import build_parser


def _stage_xsecs(software_root: Path, code_version: str, tune_dir: str) -> Path:
    """Create a fake staged xsecs.xml file and return its path."""
    dest = (
        software_root
        / "genie"
        / "genie_xsec"
        / catalog._tag_safe(code_version)
        / tune_dir
        / "xsecs.xml"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("<?xml version='1.0'?>", encoding="utf-8")
    return dest


class CatalogTests(unittest.TestCase):
    def test_version_identifier_uniform(self) -> None:
        self.assertEqual(
            catalog.version_identifier("R-3_06_00", "G18_10a_02_11a"),
            "R-3_06_00+G18_10a_02_11a",
        )

    def test_image_for_known_code_version(self) -> None:
        self.assertEqual(catalog.image_for("genie", "R-3_06_00"), "genie:R-3_06_00")
        self.assertIsNone(catalog.image_for("genie", "nope"))

    def test_ensure_compatible_accepts_valid_pair(self) -> None:
        catalog.ensure_compatible("genie", "R-3_06_00", "G18_10a_02_11a")

    def test_ensure_known_rejects_unknown_code_version(self) -> None:
        with self.assertRaises(catalog.CatalogError):
            catalog.ensure_known("genie", "R-9_99_99")

    def test_ensure_compatible_rejects_incompatible_config_version(self) -> None:
        with self.assertRaises(catalog.CatalogError):
            catalog.ensure_compatible("genie", "R-3_06_00", "not_a_real_tune")

    def test_neut_not_buildable(self) -> None:
        self.assertFalse(catalog.is_buildable("neut", "5.x"))
        self.assertTrue(catalog.is_buildable("genie", "R-3_06_00"))

    def test_genie_xsecs_xml_exact_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Compact (underscore-stripped) directory name, as the FNAL tarballs use.
            staged = _stage_xsecs(root, "R-3_06_00", "G1810a0211a")
            found = catalog.genie_xsecs_xml(root, "R-3_06_00", "G18_10a_02_11a")
            self.assertEqual(found, staged)
            self.assertIsNone(
                catalog.genie_xsecs_xml(root, "R-3_06_00", "AR23_20i_00_000")
            )

    def test_available_config_versions_filters_genie_by_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stage_xsecs(root, "R-3_06_00", "G18_10a_02_11a")
            available = catalog.available_config_versions("genie", "R-3_06_00", root)
            self.assertEqual(available, ["G18_10a_02_11a"])

    def test_available_config_versions_passthrough_for_non_genie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                catalog.available_config_versions("nuwro", "nuwro_25.11", Path(tmp)),
                catalog.config_versions("nuwro", "nuwro_25.11"),
            )


class ListGeneratorsCliTests(unittest.TestCase):
    def _run(self, argv: list[str], software_root: str | None = None) -> str:
        args = build_parser().parse_args(argv)
        buffer = io.StringIO()
        env = {"NF_SOFTWARE_ROOT": software_root} if software_root else {}
        with patch.object(catalog, "image_built", return_value=False):
            with patch.dict("os.environ", env):
                with redirect_stdout(buffer):
                    self.assertEqual(args.func(args), 0)
        return buffer.getvalue()

    def test_list_shows_only_staged_tunes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _stage_xsecs(Path(tmp), "R-3_06_00", "G18_10a_02_11a")
            output = self._run(["list-generators"], software_root=tmp)
            self.assertIn("GENERATOR", output)
            self.assertIn("genie", output)
            self.assertIn("R-3_06_00", output)
            self.assertIn("G18_10a_02_11a", output)
            # AR23 tune has no staged xsecs.xml, so it must not appear.
            self.assertNotIn("AR23_20i_00_000", output)

    def test_list_json_is_parseable(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            _stage_xsecs(Path(tmp), "R-3_06_00", "G18_10a_02_11a")
            output = self._run(
                ["list-generators", "--generator", "genie", "--json"],
                software_root=tmp,
            )
        payload = json.loads(output)
        rows = payload["generators"]
        self.assertTrue(all(row["generator"] == "genie" for row in rows))
        self.assertIn("R-3_06_00", [row["code_version"] for row in rows])
        genie_row = next(r for r in rows if r["code_version"] == "R-3_06_00")
        self.assertEqual(genie_row["config_versions"], ["G18_10a_02_11a"])

    def test_built_filter_hides_unbuilt(self) -> None:
        output = self._run(["list-generators", "--built"])
        self.assertIn("No generators match", output)


if __name__ == "__main__":
    unittest.main()
