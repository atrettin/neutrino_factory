from __future__ import annotations

import unittest

import numpy as np

from neutrino_factory.config import resolve_config
from neutrino_factory.flux import build_flux
from neutrino_factory.translators.neut import FLUX_NBINS, NeutTranslator


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
    return resolve_config({"flux": flux, "target": {"nucleus": nucleus}})


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

    def test_unknown_nucleus_raises(self) -> None:
        with self.assertRaises(KeyError):
            NeutTranslator().translate(_config(nucleus="Pb208"), _task())

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

        Events sampled proportionally to flux(E) * sigma(E) with a *constant*
        sigma reduce to sampling from the flux alone, so every energy bin must
        come back with the same sigma — the flux-averaged value. Energies are
        drawn exactly proportional to the flux density, so there is no sampling
        noise and the check is a hard equality.
        """
        config = _config()
        translated = NeutTranslator().translate(config, _task())
        sigma_avg = 0.655
        translated["flux_averaged_xsec_1e38"] = sigma_avg
        flux = build_flux(config["flux"])

        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        centers = 0.5 * (edges[:-1] + edges[1:])
        counts = np.round(contents / contents.sum() * 2_000_000).astype(int)
        energies = np.repeat(centers, counts)

        weights = NeutTranslator().compute_xsec_weight(
            energies, np.ones_like(energies), translated, flux
        )

        coarse = np.linspace(0.5, 5.0, 11)
        summed, _ = np.histogram(energies, bins=coarse, weights=weights)
        sigma_per_bin = summed / np.diff(coarse)
        np.testing.assert_allclose(sigma_per_bin, sigma_avg, rtol=2e-3)

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
