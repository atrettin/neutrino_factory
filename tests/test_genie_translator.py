from __future__ import annotations

import unittest

import numpy as np

from neutrino_factory.flux import HistogramFlux, PowerLawFlux
from neutrino_factory.translators.genie import (
    FLUX_NBINS,
    GENIE_FLUX_FILE,
    GENIE_FLUX_HIST,
    GENIE_FLUX_NBINS,
    GenieTranslator,
    _flux_grid,
)


class GenieFluxDescriptorTests(unittest.TestCase):
    def test_power_law_becomes_a_generated_histogram_not_a_tf1_string(self) -> None:
        """gevgen never samples a TF1 continuously.

        Given a function string it builds a 300-bin uniform TH1D and Monte-Carlo
        fills it with 100k entries (Apps/gEvGen.cxx, TH1FluxDriver), which is both
        coarse and Poisson-noisy at high energy. We hand it a histogram instead,
        which the ROOT-file branch clones verbatim.
        """
        spec = GenieTranslator._genie_flux_descriptor(PowerLawFlux("numu", 0.1, 50.0, -2.0))
        self.assertEqual(spec["kind"], "generated_histogram")
        self.assertEqual(spec["file"], GENIE_FLUX_FILE)
        self.assertEqual(spec["name"], GENIE_FLUX_HIST)
        self.assertEqual(spec["nbins"], GENIE_FLUX_NBINS)
        self.assertEqual(spec["spacing"], "log")

    def test_histogram_flux_is_passed_through_unchanged(self) -> None:
        flux = HistogramFlux("numu", [0.5, 1.0, 2.0], [2.0, 1.0])
        flux.source_path = "/tmp/user_flux.root"  # type: ignore[assignment]
        flux.source_name = "numu_flux"
        spec = GenieTranslator._genie_flux_descriptor(flux)
        self.assertEqual(spec["kind"], "histogram")
        self.assertEqual(spec["name"], "numu_flux")


class FluxGridTests(unittest.TestCase):
    def test_histogram_flux_keeps_its_native_binning(self) -> None:
        """The denominator must be the grid GENIE sampled on.

        GCylindTH1Flux draws with TH1::GetRandom, uniform within a bin, so the
        generated flux density is piecewise constant on exactly these edges.
        Re-binning it would divide events by a flux they were never drawn from.
        """
        edges_in = np.array([0.5, 0.8, 1.5, 3.0, 5.0])
        contents_in = np.array([4.0, 3.0, 2.0, 1.0])
        edges, contents = _flux_grid(HistogramFlux("numu", edges_in, contents_in))
        self.assertTrue(np.array_equal(edges, edges_in))
        self.assertTrue(np.array_equal(contents, contents_in))

    def test_flux_without_native_binning_falls_back_to_a_resampled_grid(self) -> None:
        edges, contents = _flux_grid(PowerLawFlux("numu", 0.5, 10.0, -2.0))
        self.assertEqual(len(contents), FLUX_NBINS)


if __name__ == "__main__":
    unittest.main()
