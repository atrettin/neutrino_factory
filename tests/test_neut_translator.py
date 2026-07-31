from __future__ import annotations

import unittest

import numpy as np

from neutrino_factory.flux import HistogramFlux, build_flux
from neutrino_factory.translators.neut import NeutTranslator

from .helpers import view_config


def _task(**overrides) -> dict:
    task = {
        "event_count": 10,
        "seed": 42,
        "code_version": "5.7.0-nuint2024",
        "config_version": "default",
        "generator_version_id": "5.7.0-nuint2024+default",
    }
    task.update(overrides)
    return task


def _config(nucleus: str = "C12", **flux_overrides) -> dict:
    flux = {
        "type": "power_law",
        "particle": "numu",
        "emin_gev": 0.5,
        "emax_gev": 5.0,
        "gamma": -2.0,
    }
    flux.update(flux_overrides)
    return view_config({"flux": flux, "target": {"nucleus": nucleus}, "generator": "neut",
                       "code_version": "5.7.0-nuint2024", "config_version": "default"})


class NeutTranslatorTests(unittest.TestCase):
    def test_translator_is_instantiable(self) -> None:
        # ConfigTranslator declares compute_xsec_weight abstract; a translator
        # that forgets to implement it cannot be constructed at all.
        self.assertIsInstance(NeutTranslator(), NeutTranslator)

    def test_card_beam_and_target_keys(self) -> None:
        card = NeutTranslator().translate(_config(), _task())["neut_card"]

        self.assertEqual(card["EVCT-NEVT"], 10)
        self.assertEqual(card["EVCT-IDPT"], 14)
        # Histogram flux driver, in GeV, pointing at the file the adapter writes.
        self.assertEqual(card["EVCT-MPV"], 3)
        self.assertEqual(card["EVCT-INMEV"], 0)
        self.assertEqual(card["EVCT-FILENM"], "'flux.root'")
        self.assertEqual(card["EVCT-HISTNM"], "'nf_flux'")
        # C12: six bound protons, six bound neutrons, no free protons.
        self.assertEqual(card["NEUT-NUMBNDP"], 6)
        self.assertEqual(card["NEUT-NUMBNDN"], 6)
        self.assertEqual(card["NEUT-NUMFREP"], 0)
        self.assertEqual(card["NEUT-NUMATOM"], 12)
        # The default config is CC, so the card runs in per-channel scaling mode
        # (see the current tests below), with a reproducible seed from $RANFILE.
        self.assertEqual(card["NEUT-MODE"], -1)
        self.assertEqual(card["NEUT-RAND"], 0)
        # Letting NEUT-CRSPATH default keeps $NEUT_CRSPATH authoritative.
        self.assertNotIn("NEUT-CRSPATH", card)

    def test_card_target_follows_nucleus(self) -> None:
        card = NeutTranslator().translate(_config(nucleus="Ar40"), _task())["neut_card"]
        self.assertEqual(card["NEUT-NUMBNDP"], 18)
        self.assertEqual(card["NEUT-NUMBNDN"], 22)
        self.assertEqual(card["NEUT-NUMATOM"], 40)

    def test_config_version_selects_model_parameters(self) -> None:
        card = NeutTranslator().translate(_config(), _task())["neut_card"]
        self.assertEqual(card["NEUT-MDLQE"], 2002)
        self.assertEqual(card["NEUT-MAQE"], 1.05)

    def test_unknown_config_version_raises(self) -> None:
        with self.assertRaises(KeyError):
            NeutTranslator().translate(_config(), _task(config_version="nieves"))

    def test_any_parsable_nucleus_is_accepted(self) -> None:
        # Composition is derived from the name rather than looked up in a short
        # table, so a nucleus NEUT itself supports needs no entry here.
        translated = NeutTranslator().translate(_config(nucleus="Pb208"), _task())
        self.assertEqual(translated["neut_card"]["NEUT-NUMBNDP"], 82)
        self.assertEqual(translated["neut_card"]["NEUT-NUMBNDN"], 126)

    def test_unparsable_nucleus_raises(self) -> None:
        with self.assertRaises(ValueError):
            NeutTranslator().translate(_config(nucleus="lead208"), _task())

    def test_translate_carries_flux_config_for_the_normalizer(self) -> None:
        translated = NeutTranslator().translate(_config(), _task())
        self.assertEqual(translated["flux_config"]["type"], "power_law")
        self.assertEqual(translated["energy_range_gev"], [0.5, 5.0])
        self.assertEqual(translated["probe"], "numu")
        self.assertEqual(translated["target"], "C12")


class NeutXsecWeightTests(unittest.TestCase):
    def test_flat_flux_hand_derivation(self) -> None:
        """With a flat flux every event carries sigma_avg / N.

        For gamma = 0 the normalized flux density is constant at
        1 / (emax - emin), so xsec_weight = sigma_avg * (emax - emin) / N is the
        same for every event regardless of its energy.
        """
        config = _config(gamma=0.0)
        translated = NeutTranslator().translate(config, _task())
        translated["flux_averaged_xsec_1e38"] = 0.8
        flux = build_flux(config["flux"])

        energies = np.array([0.6, 1.5, 3.0, 4.9])
        weights = NeutTranslator().compute_xsec_weight(
            energies, np.ones_like(energies), translated, flux
        )

        expected = 0.8 * (5.0 - 0.5) / len(energies)
        np.testing.assert_allclose(weights, expected, rtol=1e-12)

    def test_falling_flux_upweights_high_energy_events(self) -> None:
        # An E^-2 flux is 100x denser at 0.5 GeV than at 5 GeV, so an event at
        # 5 GeV must carry ~100x the weight of one at 0.5 GeV.
        config = _config()
        translated = NeutTranslator().translate(config, _task())
        translated["flux_averaged_xsec_1e38"] = 1.0
        flux = build_flux(config["flux"])

        energies = np.array([0.51, 4.99])
        weights = NeutTranslator().compute_xsec_weight(
            energies, np.ones_like(energies), translated, flux
        )
        # Not exact: the flux is evaluated on FLUX_NBINS bin centers, so the two
        # densities come from bins, not from the energies themselves.
        self.assertAlmostEqual(
            weights[1] / weights[0], (4.99 / 0.51) ** 2, delta=0.02 * (4.99 / 0.51) ** 2
        )

    def test_bin_sums_recover_the_cross_section(self) -> None:
        """The contract: sum(xsec_weight) / bin_width is sigma(E) in that bin.

        Energies are laid down exactly the way NEUT draws them, on a deliberately
        coarse, unequal-width, steeply falling grid: a bin is picked in
        proportion to its raw content (per-bin integral) and the energy is then
        uniform in E *within* that bin. With a constant sigma every flux bin must
        come back with the flux-averaged value.

        The coarse unequal binning is the point: on the equal-width flat-flux
        fixtures the other tests use, a divisor that ignored the bin widths, or
        one resampled onto a different grid, would give exactly the same answer.
        Both are wrong here, and both are what the pre-2026-07 divisor did — it
        rebuilt ``to_histogram(FLUX_NBINS)`` from the run config instead of using
        the grid NEUT was handed.
        """
        translated = NeutTranslator().translate(_config(), _task())
        sigma_avg = 0.655
        translated["flux_averaged_xsec_1e38"] = sigma_avg

        edges = np.geomspace(0.3, 30.0, 9)
        widths = np.diff(edges)
        # Exact per-bin integrals of an E^-2 density, and the density NEUT's
        # stamped histogram yields back from them.
        integrals = 1.0 / edges[:-1] - 1.0 / edges[1:]
        flux = HistogramFlux("numu", edges, integrals / widths)

        counts = np.round(integrals / integrals.sum() * 800_000).astype(int)
        energies = np.concatenate([
            lo + (np.arange(n) + 0.5) * (hi - lo) / n
            for lo, hi, n in zip(edges[:-1], edges[1:], counts)
        ])

        weights = NeutTranslator().compute_xsec_weight(
            energies, np.ones_like(energies), translated, flux
        )

        summed, _ = np.histogram(energies, bins=edges, weights=weights)
        np.testing.assert_allclose(summed / widths, sigma_avg, rtol=1e-3)
        # Sum rule: the weights integrate to sigma_avg over the whole range.
        # Not exact: the per-bin counts are rounded to whole events.
        np.testing.assert_allclose(
            float(np.sum(weights)), sigma_avg * (edges[-1] - edges[0]), rtol=1e-4
        )

    def test_missing_flux_averaged_xsec_raises(self) -> None:
        config = _config()
        translated = NeutTranslator().translate(config, _task())
        flux = build_flux(config["flux"])
        with self.assertRaises(RuntimeError) as ctx:
            NeutTranslator().compute_xsec_weight(
                np.array([1.0]), np.array([1.0]), translated, flux
            )
        self.assertIn("flux_averaged_xsec_1e38", str(ctx.exception))

    def test_empty_event_list_returns_empty_weights(self) -> None:
        config = _config()
        translated = NeutTranslator().translate(config, _task())
        translated["flux_averaged_xsec_1e38"] = 0.5
        flux = build_flux(config["flux"])
        weights = NeutTranslator().compute_xsec_weight(
            np.array([]), np.array([]), translated, flux
        )
        self.assertEqual(len(weights), 0)


if __name__ == "__main__":
    unittest.main()
