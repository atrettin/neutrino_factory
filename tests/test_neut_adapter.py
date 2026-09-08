from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.generators.neut import NeutAdapter
from neutrino_factory.translators.neut import NeutTranslator

from .helpers import view_config

CODE_VERSION = "5.7.0-nuint2024"


def _config() -> dict:
    return view_config(
        {
            "flux": {
                "type": "power_law",
                "particle": "numu",
                "emin_gev": 0.5,
                "emax_gev": 5.0,
                "gamma": -2.0,
            },
            "target": {"nucleus": "C12"},
            "generator": "neut",
            "code_version": "5.7.0-nuint2024",
            "config_version": "default",
        }
    )


def _translated(config: dict) -> dict:
    return NeutTranslator().translate(
        config,
        {
            "event_count": 10,
            "seed": 4242,
            "code_version": CODE_VERSION,
            "config_version": "default",
            "generator_version_id": f"{CODE_VERSION}+default",
        },
    )


class NeutAdapterArtifactTests(unittest.TestCase):
    def test_writes_card_flux_seed_and_sidecar(self) -> None:
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            # Pinned to the docker runtime so the command form is bare regardless
            # of whether a neutroot2 shim is on $PATH (dev image) or not.
            env = {"NF_CONTAINER_RUNTIME": "docker"}
            with patch.dict(os.environ, env, clear=False):
                with patch.object(NeutAdapter, "container_available", return_value=False):
                    command = adapter.build_run_command(_translated(config), work_dir)

            self.assertEqual(command, ["neutroot2", "neut.card", "events.neut.root"])
            for name in ("neut.card", "flux.root", "ranseed.dat", "translated_config.json"):
                self.assertTrue((work_dir / name).is_file(), name)

            card = (work_dir / "neut.card").read_text(encoding="utf-8")
            self.assertIn("EVCT-NEVT 10", card)
            self.assertIn("EVCT-MPV 3", card)
            self.assertIn("NEUT-RAND 0", card)
            # Fortran comment marker in column 1, ASCII only.
            self.assertTrue(card.startswith("C "))
            self.assertTrue(card.isascii())

    def test_seed_file_is_25_integers_in_fortran_layout(self) -> None:
        """NEUT reads $RANFILE with a (5X,5I12) format and uses the first value.

        All 25 integers must be present or the Fortran read hits end-of-file.
        """
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            with patch.object(NeutAdapter, "container_available", return_value=False):
                adapter.build_run_command(_translated(config), work_dir)

            lines = (work_dir / "ranseed.dat").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 5)
            for line in lines:
                self.assertTrue(line.startswith("     "))
                self.assertEqual(len(line), 5 + 5 * 12)
            values = [int(v) for line in lines for v in line.split()]
            self.assertEqual(len(values), 25)
            self.assertEqual(values[0], 4242)
            self.assertEqual(values[1:], [0] * 24)

    def test_flux_file_holds_log_bins_of_per_bin_integrals(self) -> None:
        """The two conventions NEUT's histogram driver depends on.

        Log spacing, because the flux bins are also the resolution of the
        reconstructed sigma(E); and per-bin *integrals* rather than densities,
        because NEUT picks a bin in proportion to its raw content and ignores
        the bin widths. Densities on an unequal-width grid would generate a
        spectrum tilted by one power of the bin width.
        """
        import uproot

        from neutrino_factory.flux import build_flux
        from neutrino_factory.translators.neut import FLUX_NBINS

        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            with patch.object(NeutAdapter, "container_available", return_value=False):
                adapter.build_run_command(_translated(config), work_dir)

            with uproot.open(work_dir / "flux.root") as f:
                contents, edges = f["nf_flux"].to_numpy()

        self.assertEqual(len(contents), FLUX_NBINS)
        self.assertAlmostEqual(edges[0], 0.5)
        self.assertAlmostEqual(edges[-1], 5.0)
        # Log spacing: a constant ratio between successive edges.
        ratios = edges[1:] / edges[:-1]
        np.testing.assert_allclose(ratios, ratios[0], rtol=1e-9)
        # Contents are density(bin center) * bin width.
        flux = build_flux(config["flux"])
        widths = np.diff(edges)
        centers = 0.5 * (edges[:-1] + edges[1:])
        expected = np.array([flux(float(c)) for c in centers]) * widths
        np.testing.assert_allclose(contents, expected, rtol=1e-12)


class NeutAdapterCommandBranchTests(unittest.TestCase):
    def test_native_binary_wins_over_available_container(self) -> None:
        # Cluster-critical branch: inside the composed Apptainer image the
        # binary is on $PATH and must be run directly (no container wrapping).
        # Pinned to the docker runtime: under apptainer the same branch emits
        # the nf-run dispatch form, which test_apptainer_dispatch.py covers.
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"NF_CONTAINER_RUNTIME": "docker"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.neut.shutil.which",
                    return_value="/opt/neut/bin/neutroot2",
                ):
                    with patch.object(NeutAdapter, "container_available", return_value=True):
                        command = adapter.build_run_command(_translated(config), Path(tmpdir))

        self.assertEqual(command[0], "neutroot2")
        self.assertNotIn("docker", command)
        self.assertNotIn("apptainer", command)

    def test_native_branch_points_ranfile_at_the_seed_file(self) -> None:
        """RANFILE must stay a bare filename, resolved against cwd.

        NEUT reads $RANFILE into an 80-character Fortran buffer and truncates
        anything longer without complaint, so an absolute path breaks as soon as
        the work root is deep (a real /ptmp task directory is ~90 characters).
        run_task launches the command with cwd=work_dir, so relative resolves.
        """
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            with patch.dict(os.environ, {}, clear=False):
                with patch(
                    "neutrino_factory.generators.neut.shutil.which",
                    return_value="/opt/neut/bin/neutroot2",
                ):
                    adapter.build_run_command(_translated(config), work_dir)
                    self.assertEqual(os.environ["RANFILE"], "ranseed.dat")
                    self.assertFalse(Path(os.environ["RANFILE"]).is_absolute())
                    self.assertLess(len(os.environ["RANFILE"]), 80)

    def test_docker_runtime_wraps_command_and_passes_ranfile(self) -> None:
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "docker"}, clear=False):
                with patch("neutrino_factory.generators.neut.shutil.which", return_value=None):
                    with patch.object(NeutAdapter, "container_available", return_value=True):
                        command = adapter.build_run_command(_translated(config), Path(tmpdir))

        self.assertEqual(command[:3], ["docker", "run", "--platform"])
        self.assertIn(f"neut:{CODE_VERSION}", command)
        # Without this the container would fall back to RANLUX's default seed and
        # every chunk would generate the same events. Relative for the same
        # 80-character-buffer reason as the native branch.
        self.assertIn("RANFILE=ranseed.dat", command)

    def test_apptainer_runtime_refuses_to_wrap(self) -> None:
        # Apptainer cannot nest: reaching the container branch without a native
        # binary means the task was launched outside its image — fail clearly.
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / f"neut_{CODE_VERSION}.sif").write_text("", encoding="utf-8")
            env = {"NF_CONTAINER_RUNTIME": "apptainer", "NF_IMAGE_ROOT": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.generators.neut.shutil.which", return_value=None):
                    with self.assertRaises(RuntimeError):
                        adapter.build_run_command(_translated(config), Path(tmpdir))


class NeutAdapterNormalizeTests(unittest.TestCase):
    def test_normalize_runs_flatten_when_only_native_output_exists(self) -> None:
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            (work_dir / "events.neut.root").write_text("", encoding="utf-8")
            raw = work_dir / "chunk.json"

            with patch.object(NeutAdapter, "_run_flatten") as flatten:
                with patch(
                    "neutrino_factory.normalizers.neut.NeutNormalizer.normalize",
                    return_value="out.h5",
                ) as normalize:
                    adapter.normalize_output(
                        raw, work_dir / "out.h5", {"code_version": CODE_VERSION}, "local"
                    )

            flatten.assert_called_once()
            self.assertEqual(normalize.call_args[0][0], work_dir / "events.flat.root")

    def test_normalize_skips_flatten_when_flat_output_exists(self) -> None:
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            (work_dir / "events.neut.root").write_text("", encoding="utf-8")
            (work_dir / "events.flat.root").write_text("", encoding="utf-8")

            with patch.object(NeutAdapter, "_run_flatten") as flatten:
                with patch(
                    "neutrino_factory.normalizers.neut.NeutNormalizer.normalize",
                    return_value="out.h5",
                ):
                    adapter.normalize_output(
                        work_dir / "chunk.json",
                        work_dir / "out.h5",
                        {"code_version": CODE_VERSION},
                        "local",
                    )

            flatten.assert_not_called()

    def test_normalize_falls_back_to_stub_json(self) -> None:
        config = _config()
        adapter = NeutAdapter(config)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            raw = work_dir / "chunk.json"
            raw.write_text("{}", encoding="utf-8")

            with patch.object(NeutAdapter, "_run_flatten") as flatten:
                with patch(
                    "neutrino_factory.normalizers.neut.NeutNormalizer.normalize",
                    return_value="out.h5",
                ) as normalize:
                    adapter.normalize_output(
                        raw, work_dir / "out.h5", {"code_version": CODE_VERSION}, "local"
                    )

            flatten.assert_not_called()
            self.assertEqual(normalize.call_args[0][0], raw)


if __name__ == "__main__":
    unittest.main()
