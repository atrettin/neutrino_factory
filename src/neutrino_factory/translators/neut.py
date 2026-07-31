from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator, physics_current
from ..flux import Flux, HistogramFlux, build_flux
from ..particles import nucleus_composition, probe_pdg

# Binning of the TH1 flux handed to NEUT. NEUT draws energies uniformly *within*
# whichever bin it picks, so this histogram is the finest structure the
# reconstructed sigma(E) can ever have: it is a resolution setting, not just a
# sampling aid. Log spacing gives constant relative resolution (~0.62%/bin over
# 0.1-50 GeV) where 500 equal-width bins gave 0.1 GeV steps that swallowed the
# whole region in which sigma(E) rises by orders of magnitude. Matches GENIE's
# GENIE_FLUX_NBINS; see the NEUT flux section of docs/design_decisions.md.
FLUX_NBINS = 1000
FLUX_SPACING = "log"

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

# Which weak current each slot of NEUT's 30-element cross-section scale arrays
# belongs to. ``NEUT-MODE -1`` ("input cross section by CRSNEUT") multiplies each
# channel's cross section by its slot's factor, so zeroing every slot of the
# unwanted current restricts generation to one current — NEUT has no CC/NC switch
# of its own, and ``NEUT-MODE n > 0`` would pin a single channel.
#
# The slot order is *not* the mode number: it is the fixed list documented in the
# NEUT-shipped cards (verified in the 5.7.0 image,
# share/neut/Cards/neut_5.4.0_nd5_O.card), and it differs between neutrinos
# (NEUT-CRS) and antineutrinos (NEUT-CRSB), which carry separate free/bound CCQE
# slots. Both rows are written on every card, each masked with its own table, so
# the run does not depend on which array NEUT consults for a given beam sign.
#
# The card labels slots 14/15 (nu) and 15/16 (nubar) only as "coherent"; they are
# read as CC-then-NC, the CC-before-NC ordering every other pair in the list
# follows (eta, K, 1 gamma, DIS, diffractive). Slot 22 for neutrinos is "N/A" and
# is left at zero in both masks.
_CC, _NC = "cc", "nc"
CRS_SLOT_CURRENTS: dict[str, tuple[str | None, ...]] = {
    # 1 CCQE | 2-4 CC 1pi | 5 CC DIS 1320 | 6-9 NC 1pi | 10 NC DIS 1320 |
    # 11-13 NC elastic | 14/15 coherent | 16 CC eta | 17,18 NC eta | 19 CC K |
    # 20,21 NC K | 22 N/A | 23 CC DIS | 24 NC DIS | 25 CC 1gamma | 26,27 NC
    # 1gamma | 28 CC 2p2h | 29 CC diffractive | 30 NC diffractive
    "nu": (
        _CC, _CC, _CC, _CC, _CC, _NC, _NC, _NC, _NC, _NC,
        _NC, _NC, _NC, _CC, _NC, _CC, _NC, _NC, _CC, _NC,
        _NC, None, _CC, _NC, _CC, _NC, _NC, _CC, _CC, _NC,
    ),
    # As above, but 11 is CCQE (bound), pushing NC elastic to 12-14 and the
    # coherent pair to 15/16.
    "nubar": (
        _CC, _CC, _CC, _CC, _CC, _NC, _NC, _NC, _NC, _NC,
        _CC, _NC, _NC, _NC, _CC, _NC, _CC, _NC, _NC, _CC,
        _NC, _NC, _CC, _NC, _CC, _NC, _NC, _CC, _CC, _NC,
    ),
}


def _flux_grid(flux: Flux) -> tuple[np.ndarray, np.ndarray]:
    """Return the ``(bin_edges, bin_contents)`` grid to divide the flux out on.

    For a :class:`HistogramFlux` this is the histogram's *native* binning — for
    NEUT that means the ``flux_<flavour>`` histogram NEUT stamped into its own
    output, which is the input TH1 copied verbatim. Re-binning it onto any other grid
    would divide the events by a flux they were never drawn from. The same
    helper, and the same reasoning, as ``translators/genie.py::_flux_grid``.

    Only a flux with no native binning falls back to a resampled grid.
    """
    if isinstance(flux, HistogramFlux):
        return flux.bin_edges, flux.bin_contents
    return flux.to_histogram(nbins=FLUX_NBINS, spacing=FLUX_SPACING)


class NeutTranslator(ConfigTranslator):
    name = "neut"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        particle_pdg = probe_pdg(particle, "NEUT")
        nucleus = target["nucleus"]
        protons, neutrons = nucleus_composition(nucleus)

        current = physics_current(config)

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
            "probe_pdg": particle_pdg,
            "target": nucleus,
            "energy_range_gev": [flux.emin_gev, flux.emax_gev],
            "events": int(task["event_count"]),
            "seed": int(task["seed"]),
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "flux_nbins": FLUX_NBINS,
            "flux_spacing": FLUX_SPACING,
            "current": current,
            "physics_mode": config["physics"].get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": config_version,
            "generator_version_id": task.get("generator_version_id"),
            "neut_card": self._card(
                task, particle, protons, neutrons, config_version, current
            ),
        }

    @staticmethod
    def _crs_row(slot_currents: tuple[str | None, ...], current: str) -> str:
        """One ``NEUT-CRS``/``NEUT-CRSB`` row: 1. for wanted slots, 0. elsewhere."""
        # Slots belonging to another current — and the one "N/A" slot, which is
        # None — are switched off.
        return " ".join("1." if slot == current else "0." for slot in slot_currents)

    @staticmethod
    def _card(
        task: dict[str, Any],
        particle: str,
        protons: int,
        neutrons: int,
        config_version: str,
        current: str,
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

        ``NEUT-MODE`` carries the weak current: ``0`` is NEUT's normal mode, in
        which every channel is generated in proportion to its cross section
        (``inclusive``), while ``-1`` scales each channel by its ``NEUT-CRS``
        slot, which is how a single current is selected (see
        ``CRS_SLOT_CURRENTS``).
        """
        card: dict[str, Any] = {
            "EVCT-NEVT": int(task["event_count"]),
            "EVCT-IDPT": probe_pdg(particle, "NEUT"),
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
        if current != "inclusive":
            card["NEUT-MODE"] = -1
            card["NEUT-CRS"] = NeutTranslator._crs_row(CRS_SLOT_CURRENTS["nu"], current)
            card["NEUT-CRSB"] = NeutTranslator._crs_row(
                CRS_SLOT_CURRENTS["nubar"], current
            )
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

        ``flux`` must be the spectrum NEUT *actually sampled* — the ``flux_<flavour>``
        histogram it stamps into its own output, loaded on its native binning by
        ``NeutNormalizer._generated_flux`` — not a flux rebuilt from the run
        config. ``_flux_grid`` keeps that binning intact.

        Verified directly against NEUT 5.7.0 rather than assumed: the generated
        energy spectrum tracks the ``evtrt`` (flux x sigma) histogram NEUT writes
        into its own output and not the ``flux`` histogram — over ten coarse
        bins the summed absolute difference in normalized shape was 0.034
        against ``evtrt`` versus 0.95 against ``flux``.

        How NEUT draws an energy, and why ``flux`` must be the histogram NEUT was
        actually handed (``neutroot2``'s ``rndenuevtrt_`` -> ``Ufm2TH1dist``,
        whose ``Init`` calls ``TH1::ComputeIntegral``/``GetIntegral`` and whose
        ``GetValue`` inverts that cumulative — ``TH1::GetRandom`` semantics,
        applied to the ``evtrt`` histogram):

        * A bin is chosen in proportion to its **raw content**, with the bin
          widths ignored — ``ComputeIntegral`` normalizes by the sum of the
          contents. Measured on a deliberately unequal-width grid (10 log-spaced
          bins over 0.1-50 GeV, 30k events): chi2/ndf 0.90 against
          ``p_b ~ evtrt_b``, 1.7e4 against ``p_b ~ evtrt_b * width_b``. The
          adapter therefore writes per-bin *integrals*, not densities, into the
          TH1 — see ``NeutAdapter._write_flux_file``.
        * Within the chosen bin the energy is **uniform in E**, not in log E.
          Same run, splitting each flux bin into 8 slices: chi2/ndf between 0.4
          and 2.9 against a flat density, in bins spanning up to a factor 1.9 in
          energy. So the generated flux density is piecewise constant on exactly
          the input bin edges, and the reconstructed sigma(E) is a staircase on
          that same grid — which is why ``FLUX_NBINS`` is a resolution setting.

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

        edges, contents = _flux_grid(flux)
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
