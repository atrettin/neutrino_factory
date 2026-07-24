from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory import containers


class RuntimeSelectionTests(unittest.TestCase):
    def test_explicit_runtime_wins(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}):
            self.assertEqual(containers.runtime(), "apptainer")
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "docker"}):
            self.assertEqual(containers.runtime(), "docker")

    def test_auto_prefers_docker_then_apptainer(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "auto"}):
            with patch("neutrino_factory.containers.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
                self.assertEqual(containers.runtime(), "docker")
            with patch(
                "neutrino_factory.containers.shutil.which",
                side_effect=lambda name: "/usr/bin/apptainer" if name == "apptainer" else None,
            ):
                self.assertEqual(containers.runtime(), "apptainer")
            with patch("neutrino_factory.containers.shutil.which", return_value=None):
                self.assertEqual(containers.runtime(), "none")


class SifPathTests(unittest.TestCase):
    def test_sif_name_is_filesystem_safe(self) -> None:
        self.assertEqual(containers.sif_name("genie:R-3_06_00"), "genie_R-3_06_00.sif")
        self.assertEqual(containers.sif_name("nf-base"), "nf-base.sif")

    def test_sif_path_uses_image_root(self) -> None:
        with patch.dict(os.environ, {"NF_IMAGE_ROOT": "/ptmp/images"}):
            self.assertEqual(
                containers.sif_path("gibuu:release2025"),
                Path("/ptmp/images/gibuu_release2025.sif"),
            )


class ImageAvailableTests(unittest.TestCase):
    def test_apptainer_availability_is_sif_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"NF_CONTAINER_RUNTIME": "apptainer", "NF_IMAGE_ROOT": tmpdir}
            with patch.dict(os.environ, env):
                self.assertFalse(containers.image_available("genie:R-3_06_00"))
                (Path(tmpdir) / "genie_R-3_06_00.sif").write_text("", encoding="utf-8")
                self.assertTrue(containers.image_available("genie:R-3_06_00"))

    def test_none_runtime_reports_unavailable(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "none"}):
            self.assertFalse(containers.image_available("genie:R-3_06_00"))

    def test_empty_image_is_unavailable(self) -> None:
        self.assertFalse(containers.image_available(None))
        self.assertFalse(containers.image_available(""))


class ApptainerDispatchTests(unittest.TestCase):
    def test_rewrites_to_nf_run_under_apptainer(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}):
            self.assertEqual(
                containers.apptainer_dispatch("genie", "R-3_06_00", ["gevgen", "-n", "1"]),
                ["nf-run", "genie", "R-3_06_00", "gevgen", "-n", "1"],
            )

    def test_unchanged_under_docker_and_none(self) -> None:
        for rt in ("docker", "none"):
            with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": rt}):
                self.assertEqual(
                    containers.apptainer_dispatch("genie", "R-3_06_00", ["gevgen", "-n", "1"]),
                    ["gevgen", "-n", "1"],
                )

    def test_empty_args_and_missing_version_fall_back(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}):
            self.assertEqual(containers.apptainer_dispatch("genie", "R-3_06_00", []), [])
            self.assertEqual(
                containers.apptainer_dispatch("genie", None, ["gevgen"]), ["gevgen"]
            )
            self.assertEqual(
                containers.apptainer_dispatch("genie", "", ["gevgen"]), ["gevgen"]
            )

    def test_prefix_shell_variant(self) -> None:
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}):
            self.assertEqual(
                containers.apptainer_dispatch_prefix("gibuu", "release2025", "GiBUU.x"),
                "nf-run gibuu release2025 GiBUU.x",
            )
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "docker"}):
            self.assertEqual(
                containers.apptainer_dispatch_prefix("gibuu", "release2025", "GiBUU.x"),
                "GiBUU.x",
            )
        with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}):
            self.assertEqual(
                containers.apptainer_dispatch_prefix("gibuu", None, "GiBUU.x"), "GiBUU.x"
            )


class DockerWrapTests(unittest.TestCase):
    def test_wrap_shape_with_ro_bind_and_workdir(self) -> None:
        command = containers.docker_wrap(
            "genie:R-3_06_00",
            ["gevgen", "-n", "10"],
            [("/host/xsec", "/genie_xsec", "ro"), ("/host/work", "/work")],
            "/work",
        )
        self.assertEqual(
            command,
            [
                "docker", "run", "--platform", "linux/amd64", "--rm",
                "-v", "/host/xsec:/genie_xsec:ro",
                "-v", "/host/work:/work",
                "-w", "/work",
                "genie:R-3_06_00",
                "gevgen", "-n", "10",
            ],
        )


if __name__ == "__main__":
    unittest.main()
