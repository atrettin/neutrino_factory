from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import numpy as np

from neutrino_factory.flux import (
    FluxError,
    HistogramFlux,
    PowerLawFlux,
    build_flux,
    validate_flux,
)


def _write_root_histogram(path: Path, name: str, edges, contents) -> None:
    import uproot

    with uproot.recreate(path) as handle:
        handle[name] = (np.asarray(contents, dtype=np.float64), np.asarray(edges, dtype=np.float64))


class PowerLawFluxTests(unittest.TestCase):
    def test_call_shape_and_bounds(self) -> None:
        flux = PowerLawFlux("numu", 0.5, 10.0, -2.0)
        self.assertAlmostEqual(flux(2.0), 2.0 ** -2.0)
        self.assertGreater(flux(1.0), flux(4.0))  # falling spectrum
        self.assertEqual(flux(0.1), 0.0)  # below range
        self.assertEqual(flux(20.0), 0.0)  # above range

    def test_sample_energies_within_range_and_falling(self) -> None:
        flux = PowerLawFlux("numu", 0.5, 5.0, -2.0)
        rng = random.Random(1)
        energies = flux.sample_energies(2000, rng)
        self.assertTrue(all(0.5 <= e <= 5.0 for e in energies))
        # Falling spectrum: mean well below the midpoint (2.75).
        self.assertLess(np.mean(energies), 2.0)


class HistogramFluxTests(unittest.TestCase):
    def test_call_returns_bin_content(self) -> None:
        flux = HistogramFlux("numu", [0.5, 1.0, 2.0, 4.0], [10.0, 4.0, 1.0])
        self.assertEqual(flux.emin_gev, 0.5)
        self.assertEqual(flux.emax_gev, 4.0)
        self.assertEqual(flux(0.7), 10.0)
        self.assertEqual(flux(1.5), 4.0)
        self.assertEqual(flux(3.0), 1.0)
        self.assertEqual(flux(0.2), 0.0)
        self.assertEqual(flux(5.0), 0.0)

    def test_mismatched_lengths_raise(self) -> None:
        with self.assertRaises(FluxError):
            HistogramFlux("numu", [0.5, 1.0, 2.0], [1.0, 2.0, 3.0])

    def test_from_root_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "flux.root"
            _write_root_histogram(path, "numu_flux", [0.5, 1.0, 2.0, 4.0], [10.0, 4.0, 1.0])
            flux = HistogramFlux.from_root_file(path, "numu_flux", "numu")
        self.assertEqual(flux.emin_gev, 0.5)
        self.assertEqual(flux.emax_gev, 4.0)
        self.assertEqual(flux(1.5), 4.0)
        self.assertEqual(flux.source_name, "numu_flux")
        assert flux.source_path is not None
        self.assertEqual(flux.source_path.name, "flux.root")


class BuildFluxTests(unittest.TestCase):
    def test_build_power_law(self) -> None:
        flux = build_flux({"type": "power_law", "particle": "numu", "emin_gev": 0.5, "emax_gev": 10.0, "gamma": -2.0})
        self.assertIsInstance(flux, PowerLawFlux)

    def test_build_histogram_resolves_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "flux.root"
            _write_root_histogram(path, "numu_flux", [0.5, 1.0, 2.0], [3.0, 1.0])
            flux = build_flux(
                {"type": "histogram", "particle": "numu", "histogram_file": "flux.root", "histogram_name": "numu_flux"},
                base_dir=tmpdir,
            )
        self.assertIsInstance(flux, HistogramFlux)

    def test_unknown_type_raises(self) -> None:
        with self.assertRaises(FluxError):
            build_flux({"type": "banana"})


class ValidateFluxTests(unittest.TestCase):
    def test_valid_power_law(self) -> None:
        self.assertEqual(validate_flux({"type": "power_law", "emin_gev": 0.5, "emax_gev": 10.0, "gamma": -2.0}), [])

    def test_power_law_bad_bounds(self) -> None:
        errors = validate_flux({"type": "power_law", "emin_gev": 10.0, "emax_gev": 1.0, "gamma": -2.0})
        self.assertTrue(any("emax_gev must be >=" in e for e in errors))

    def test_power_law_missing_gamma(self) -> None:
        errors = validate_flux({"type": "power_law", "emin_gev": 0.5, "emax_gev": 10.0})
        self.assertTrue(any("gamma" in e for e in errors))

    def test_histogram_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "flux.root"
            _write_root_histogram(path, "numu_flux", [0.5, 1.0, 2.0], [3.0, 1.0])
            errors = validate_flux(
                {"type": "histogram", "histogram_file": str(path), "histogram_name": "numu_flux"}
            )
        self.assertEqual(errors, [])

    def test_histogram_missing_file(self) -> None:
        errors = validate_flux(
            {"type": "histogram", "histogram_file": "/nonexistent/flux.root", "histogram_name": "numu_flux"}
        )
        self.assertTrue(any("not found" in e for e in errors))

    def test_histogram_negative_contents_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "flux.root"
            _write_root_histogram(path, "numu_flux", [0.5, 1.0, 2.0], [3.0, -1.0])
            errors = validate_flux(
                {"type": "histogram", "histogram_file": str(path), "histogram_name": "numu_flux"}
            )
        self.assertTrue(any("non-negative" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
