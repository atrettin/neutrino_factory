from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator
from .nuwro import NUCLEUS_COMPOSITION, PARTICLE_PDG
from ..flux import Flux, build_flux

# Number of equal-width bins used both for the TH1 flux handed to NEUT and for
# the flux-average in compute_xsec_weight. Matches the other translators so the
# binning used to *generate* and to *reweight* is identical by construction.
FLUX_NBINS = 500

# Names of the files the adapter writes into the task work directory. The card
# references them by name, so translator and adapter must agree.
FLUX_FILE = "flux.root"
FLUX_HIST = "nf_flux"

# NEUT's card knob for the CCQE/NCEL model and axial mass. These two values are
# what config_version "default" means; they match the NEUT-shipped
# neut_5.4.0_nd5_* cards (Smith-Moniz + BBBA05, RPA, Nieves 1p1h).
DEFAULT_MDLQE = 2002
DEFAULT_MAQE = 1.05

# config_version -> the physics-model card keys it selects. Only "default"
# exists for now (see .claude/TODOS.md); a new parameter set is a new entry
# here plus a new config_versions entry on NeutAdapter.
CONFIG_VERSION_CARDS: dict[str, dict[str, Any]] = {
    "default": {"NEUT-MDLQE": DEFAULT_MDLQE, "NEUT-MAQE": DEFAULT_MAQE},
}


class NeutTranslator(ConfigTranslator):
    name = "neut"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        if particle not in PARTICLE_PDG:
            raise KeyError(
                f"Unknown neutrino particle '{particle}' for NEUT probe. "
                f"Known: {', '.join(PARTICLE_PDG)}"
            )
        nucleus = target["nucleus"]
        if nucleus not in NUCLEUS_COMPOSITION:
            raise KeyError(
                f"Unknown target nucleus '{nucleus}' for NEUT. "
                f"Known: {', '.join(NUCLEUS_COMPOSITION)}"
            )
        protons, neutrons = NUCLEUS_COMPOSITION[nucleus]

        config_version = str(task["config_version"])
        if config_version not in CONFIG_VERSION_CARDS:
            raise KeyError(
                f"No NEUT card parameters defined for config_version "
                f"'{config_version}'. Known: {', '.join(CONFIG_VERSION_CARDS)}"
            )

        return {
            "generator": self.name,
            "command": "neutroot2",
            "probe": particle,
            "probe_pdg": PARTICLE_PDG[particle],
            "target": nucleus,
            "energy_range_gev": [flux.emin_gev, flux.emax_gev],
            "events": int(task["event_count"]),
            "seed": int(task["seed"]),
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "flux_nbins": FLUX_NBINS,
            # NEUT's NEUT-MODE selects a *single* interaction channel, not a
            # current, so there is no way to restrict a run to CC or NC. The
            # setting is recorded but not honoured, as in the NuWro translator.
            "current": config["physics"].get("current", "cc"),
            "physics_mode": config["physics"].get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": config_version,
            "generator_version_id": task.get("generator_version_id"),
            "neut_card": self._card(task, particle, protons, neutrons, config_version),
        }

    @staticmethod
    def _card(
        task: dict[str, Any],
        particle: str,
        protons: int,
        neutrons: int,
        config_version: str,
    ) -> dict[str, Any]:
        """Build NEUT's card as an ordered ``key -> value`` mapping.

        NEUT reads a plain-text card of ``KEY value`` lines (``C``-prefixed
        comments); the adapter renders this mapping verbatim. Two unit traps,
        both confirmed against NEUT 5.7.0:

        * ``EVCT-PV`` (the fixed/uniform energy range, unused here) is in MeV,
          whereas the ``EVCT-FILENM``/``EVCT-HISTNM`` flux histogram is read in
          the unit declared by ``EVCT-INMEV`` — 0 for GeV, which is what
          :meth:`Flux.to_histogram` produces.
        * ``EVCT-MPV 3`` (histogram flux) is used for *every* framework flux
          type. Unlike GENIE, NEUT has no function-flux driver, and
          ``Flux.to_histogram`` already reduces power-law and histogram fluxes
          to the same representation.

        ``NEUT-RAND 0`` makes NEUT seed RANLUX from the file named by the
        ``RANFILE`` environment variable (the adapter writes it); ``1`` would
        seed from the clock and destroy chunk reproducibility. ``NEUT-CRSPATH``
        is deliberately omitted so the cross-section tables resolve from
        ``$NEUT_CRSPATH``, which the container images already set.
        """
        card: dict[str, Any] = {
            "EVCT-NEVT": int(task["event_count"]),
            "EVCT-IDPT": PARTICLE_PDG[particle],
            # Vertex position and beam direction are irrelevant to the common
            # output, so pin them rather than leaving them to NEUT's defaults.
            "EVCT-MPOS": 1,
            "EVCT-POS": "0. 0. 0.",
            "EVCT-MDIR": 1,
            "EVCT-DIR": "0. 0. 1.",
            "EVCT-MPV": 3,
            "EVCT-FILENM": f"'{FLUX_FILE}'",
            "EVCT-HISTNM": f"'{FLUX_HIST}'",
            "EVCT-INMEV": 0,
            "NEUT-NUMBNDN": neutrons,
            "NEUT-NUMBNDP": protons,
            # The framework's targets are all pure nuclei (no free-proton
            # component as in a CH or H2O composite target).
            "NEUT-NUMFREP": 0,
            "NEUT-NUMATOM": protons + neutrons,
            "NEUT-MODE": 0,
            "NEUT-RAND": 0,
        }
        card.update(CONFIG_VERSION_CARDS[config_version])
        return card

    def compute_xsec_weight(
        self,
        energies_gev: np.ndarray,
        raw_weights: np.ndarray,
        translated_config: dict[str, Any],
        flux: Flux,
    ) -> np.ndarray:
        """Recover the physical, energy-resolved cross section from NEUT output.

        NEUT generates *unweighted* events, so ``raw_weights`` carries no
        normalization information (the normalizer passes ones); it is accepted
        for interface parity with :class:`ConfigTranslator` and unused. Events
        are distributed in energy with density proportional to
        ``flux(E) * sigma_total(E)``, exactly as documented for GENIE in
        ``GenieTranslator.compute_xsec_weight``, so the same reweighting applies:
        divide the run's flux-averaged total cross section by the number of
        events and by the unit-normalized flux density at each event's energy.

        Verified directly against NEUT 5.7.0 rather than assumed: the generated
        energy spectrum tracks the ``evtrt`` (flux x sigma) histogram NEUT writes
        into its own output and not the ``flux`` histogram — over ten coarse
        bins the summed absolute difference in normalized shape was 0.034
        against ``evtrt`` versus 0.95 against ``flux``.

        Unlike GENIE, the flux-averaged constant needs no external spline file:
        NEUT stamps both the flux histogram it sampled and the resulting event
        rate into the output, and the ratio of their integrals *is* the
        flux-averaged total cross section. The normalizer reads those histograms
        and injects the ratio here as ``flux_averaged_xsec_1e38``.

        That quantity is per *nucleon* and already in units of 1e-38 cm^2 — no
        ``XSEC_SCALE`` and no division by the mass number, unlike GENIE's
        whole-nucleus splines. Two independent checks:

        * It does not scale with A. Regenerating the same flux on C12, O16,
          Ar40 and CH gives 0.655, 0.663, 0.675 and 0.636 respectively — the
          small spread of a per-nucleon quantity responding to isospin content,
          not the factor ~3.3 a whole-nucleus quantity would show between C12
          and Ar40.
        * NUISANCE, reading the same file through its own NEUT input handler,
          reports ``Event/Flux : 6.55288e-39 cm2/nucleon`` where this ratio is
          0.6552880 — agreement to every printed digit.
        """
        if "flux_averaged_xsec_1e38" not in translated_config:
            raise RuntimeError(
                "translated_config is missing 'flux_averaged_xsec_1e38'. NEUT's "
                "flux-averaged cross section is read from the flux/evtrt "
                "histograms in the generator output and injected by "
                "NeutNormalizer; compute_xsec_weight cannot be called without it."
            )
        flux_averaged_xsec = float(translated_config["flux_averaged_xsec_1e38"])

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        xsec_weight = np.zeros_like(energies_gev)

        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        widths = np.diff(edges)
        clipped = np.clip(contents, 0.0, None)
        integral = float(np.sum(clipped * widths))
        if integral <= 0.0:
            return xsec_weight

        n_events = len(energies_gev)
        if n_events == 0:
            return xsec_weight

        bin_index = np.clip(
            np.searchsorted(edges, energies_gev, side="right") - 1, 0, len(clipped) - 1
        )
        flux_density = clipped[bin_index]
        nonzero = flux_density > 0.0
        flux_hat = flux_density[nonzero] / integral
        xsec_weight[nonzero] = flux_averaged_xsec / (n_events * flux_hat)
        return xsec_weight

    def xsec_norm_count(
        self, translated_config: dict[str, Any], event_count: int
    ) -> float:
        """The chunk's event count — the ``n_events`` divided out above.

        NEUT is unweighted, so each event is one sample of the same estimator and
        a chunk's statistical size is simply how many events it holds.
        """
        return float(event_count)
