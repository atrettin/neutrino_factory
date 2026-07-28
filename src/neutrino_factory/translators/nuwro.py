from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator
from ..flux import Flux, build_flux

# Number of equal-width bins used to approximate a continuous spectrum as a
# NuWro `beam_energy` histogram. NuWro's parser caps at 5000 bins.
FLUX_NBINS = 500

# Cross section unit scale: raw NuWro weights are a plain cross section value
# in cm^2 (see compute_xsec_weight); this rescales to the common "1e-38 cm^2"
# convention so numbers stay O(1) near 1 GeV instead of O(1e-38).
XSEC_SCALE = 1e38

# PDG codes for NuWro's beam_particle parameter.
PARTICLE_PDG = {
    "numu": 14,
    "nue": 12,
    "numubar": -14,
    "nuebar": -12,
    "nutau": 16,
    "nutaubar": -16,
}

# (protons, neutrons) for NuWro's nucleus_p / nucleus_n parameters.
NUCLEUS_COMPOSITION = {
    "Ar40": (18, 22),
    "C12": (6, 6),
    "O16": (8, 8),
    "Fe56": (26, 30),
    "Ca40": (20, 20),
}


class NuWroTranslator(ConfigTranslator):
    name = "nuwro"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        nucleus = target["nucleus"]
        energy_range_gev = [flux.emin_gev, flux.emax_gev]
        seed = int(task["seed"])

        protons, neutrons = NUCLEUS_COMPOSITION[nucleus]

        # NuWro (beam_type=0) reads the spectrum straight from `beam_energy`.
        # target_type=0 selects a single nucleus via nucleus_p / nucleus_n.
        nuwro_params = {
            "number_of_events": int(task["event_count"]),
            "random_seed": seed,
            "beam_particle": PARTICLE_PDG[particle],
            "beam_type": 0,
            "beam_energy": self._beam_energy(flux),
            "nucleus_p": protons,
            "nucleus_n": neutrons,
            "target_type": 0,
        }

        return {
            "generator": self.name,
            "command": "nuwro",
            "beam_particle": particle,
            "nucleus": nucleus,
            "energy_range_gev": energy_range_gev,
            "number_of_events": int(task["event_count"]),
            "seed": seed,
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "mode": config["physics"].get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
            "nuwro_params": nuwro_params,
        }

    def compute_xsec_weight(
        self,
        energies_gev: np.ndarray,
        raw_weights: np.ndarray,
        translated_config: dict[str, Any],
        flux: Flux,
    ) -> np.ndarray:
        """Recover the physical, energy-resolved cross section from NuWro output.

        NuWro rejection-samples: every accepted event's raw ``weight`` (the
        ``e/weight`` ROOT branch) is the same constant value for the whole
        run - the flux-averaged total cross section, in cm^2. This is set in
        NuWro's production event loop (``NuWro::real_events``, ``src/nuwro.cc``):
        ``e->weight = _procesy.total();``, where ``chooser::total()``
        (``src/chooser.h``) sums the flux-averaged mean cross section over all
        active dynamics channels. The energy dependence of the physical
        process lives entirely in how many events land in each energy bin
        (events are sampled with density proportional to flux(E) * sigma(E)),
        not in the per-event weight value.

        This raw weight is already a per-target-nucleon quantity, not a
        whole-nucleus total: each event's target nucleon is drawn by
        ``nucleus::get_nucleon()`` (``src/nucleus.cc``) as a single
        representative nucleon of the whole nucleus, chosen proton vs.
        neutron with probability equal to its isotopic fraction
        (``frac_proton()``/``frac_neutron()``) - i.e. uniformly over all A
        nucleons - and no compensating factor of A is applied anywhere in
        ``qelevent1.cc``/``makeevent()`` afterwards. So the ensemble average
        of the raw weight already is the isospin-weighted per-nucleon cross
        section (confirmed empirically: NuWro's own console total, rescaled
        by ``XSEC_SCALE``, comes out to ~1 near 1 GeV - exactly the expected
        per-nucleon magnitude). Dividing by the target nucleon count again
        would double-count this normalization, so this method does not do so.

        Recovering dsigma/dE(E) means dividing each event's raw weight by the
        (normalized-to-unit-integral) flux density at its own energy and by
        the number of events - i.e. the raw weight's inverse flux, matching
        the standard "weight = inverse of the flux" recipe for
        rejection-sampled generators.
        """
        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        widths = np.diff(edges)
        clipped = np.clip(contents, 0.0, None)
        integral = float(np.sum(clipped * widths))

        xsec_weight = np.zeros_like(raw_weights, dtype=np.float64)
        if integral <= 0.0:
            return xsec_weight

        bin_index = np.clip(
            np.searchsorted(edges, energies_gev, side="right") - 1, 0, len(clipped) - 1
        )
        flux_density = clipped[bin_index]
        n_events = len(energies_gev)
        nonzero = flux_density > 0.0
        flux_hat = flux_density[nonzero] / integral
        xsec_weight[nonzero] = raw_weights[nonzero] * XSEC_SCALE / (n_events * flux_hat)
        return xsec_weight

    def xsec_norm_count(
        self, translated_config: dict[str, Any], event_count: int
    ) -> float:
        """The chunk's event count — the ``n_events`` divided out above.

        NuWro is unweighted/rejection-sampled, so each event is one sample of the
        same estimator and a chunk's statistical size is simply how many events
        it holds.
        """
        return float(event_count)

    @staticmethod
    def _beam_energy(flux: Any) -> str:
        """Render NuWro's `beam_energy` value (MeV) from the framework flux.

        NuWro (beam_type=0) encodes the spectrum inline: a single value is
        monoenergetic, while ``E0 E1 a0 a1 ... a(n-1)`` is a histogram with
        ``n`` equal-width bins over ``[E0, E1]`` and (unnormalized) bin weights
        ``a_i``. This matches ``Flux.to_histogram`` exactly (equidistant edges).
        Energies are in MeV, so GeV values are scaled by 1000.
        """
        emin_mev = flux.emin_gev * 1000.0
        emax_mev = flux.emax_gev * 1000.0
        if flux.emax_gev <= flux.emin_gev:
            # Degenerate range -> monoenergetic beam.
            return f"{emin_mev}"
        _edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        weights = " ".join(str(float(w)) for w in contents)
        return f"{emin_mev} {emax_mev} {weights}"
