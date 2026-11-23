from __future__ import annotations

import json
import re
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
# GENIE_FLUX_NBINS; see the flux section of docs/generators/neut.md.
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
# exists for now; a new parameter set is a new entry
# here plus a new config_versions entry on NeutAdapter.
CONFIG_VERSION_CARDS: dict[str, dict[str, Any]] = {
    "default": {"NEUT-MDLQE": DEFAULT_MDLQE, "NEUT-MAQE": DEFAULT_MAQE},
}

# NEUT 6 (TOML card) config versions -> overrides merged under the card's [neut]
# table, on top of NEUT 6's shipped NEUT.Defaults.toml. Names follow the T2K
# model sets of the NEUT 6.2 release slides (docs/generators/neut.md, "Model
# sets"): B25 is the shipped defaults with hybrid FSI Pauli blocking, which
# NEUT.Defaults.toml leaves "on". A25 / A25 without SPP Eb need ED-RMF tensors
# from the private neut-data repo, so they are not offered.
NEUT6_CONFIG_VERSIONS: dict[str, dict[str, Any]] = {
    "b25": {"target": {"nuclear_effects": {"pauli_blocking_fsi": "hybrid"}}},
}


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Recursively merge ``overrides`` into ``base`` in place."""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value

# Which weak current each slot of NEUT's 30-element cross-section scale arrays
# belongs to. ``NEUT-MODE -1`` ("input cross section by CRSNEUT") multiplies each
# channel's cross section by its slot's factor, so zeroing every slot of the
# unwanted current restricts generation to one current — NEUT has no CC/NC switch
# of its own, and ``NEUT-MODE n > 0`` would pin a single channel.
#
# The slot order is *not* the mode number. A slot is an index into a dense
# 30-element scaling array, ordered by NEUT's internal channel enumeration
# (necard.h: "Multiplied factor to cross section on each mode. See nemodsel.F");
# a mode is the sparse per-event channel ID (CC <= 30, NC >= 31). Slots also
# split some channels by free vs. bound nucleon where the mode does not, so the
# map is not even injective: nu slots 11 and 12 both produce mode 51. The two
# arrays differ from each other because the nubar list carries separate
# free/bound CCQE slots (1 and 11), shifting NC elastic and coherent by one.
# Both rows are written on every card, each masked with its own table, so the run
# does not depend on which array NEUT consults for a given beam sign. Slot 22 for
# neutrinos has no channel and is left at zero in both masks.
#
# The slot -> mode map was measured on NEUT 5.7.0 (each slot enabled alone under
# NEUT-MODE -1, 400 events, observed mode read off the flattened tree); deriving
# each slot's current from its mode reproduces both tables below with zero
# mismatches, including the coherent pairs, which the cards label ambiguously as
# just "coherent" and which resolve CC-then-NC. Full table in
# docs/generators/neut.md.
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


# NEUT 6 replaces the slot arrays above with a named ``mode_rescaling`` table,
# read under ``mode_choice_type = "rescaled"`` into the same CRSNEUT/CRSNEUTB
# arrays (and NEMODFLG -1) that NEUT-CRS/NEUT-CRSB fill in NEUT 5. NEUT reads every
# one of these names and aborts on a missing one; the current is the name's
# prefix. Copied from ModeNameMapping_nu/_nub in NEUT 6.2.4's
# src/config/CardSwitchValues.cc (nu slot 22 has no name, as above).
NEUT6_MODE_NAMES: tuple[str, ...] = (
    "CC_QE_nu", "CC_RES_ppi+_nu", "CC_RES_ppi0_nu", "CC_RES_npi+_nu",
    "CC_multi_pi_nu", "NC_RES_npi0_nu", "NC_RES_ppi0_nu", "NC_RES_ppi-_nu",
    "NC_RES_npi+_nu", "NC_multi_pi_nu", "NC_elastic_free_p_nu",
    "NC_elastic_bound_p_nu", "NC_elastic_n_nu", "CC_COH_nu", "NC_COH_nu",
    "CC_eta_nu", "NC_eta_n_nu", "NC_eta_p_nu", "CC_kaon_nu", "NC_kaon_n_nu",
    "NC_kaon_p_nu", "CC_DIS_nu", "NC_DIS_nu", "CC_1gamma_nu", "NC_1gamma_n_nu",
    "NC_1gamma_p_nu", "CC_2p2h_nu", "CC_DIF_nu", "NC_DIF_nu",
    "CC_QE_free_proton_nubar", "CC_RES_npi-_nubar", "CC_RES_ppi0_nubar",
    "CC_RES_ppi-_nubar", "CC_multi_pi_nubar", "NC_RES_npi0_nubar",
    "NC_RES_ppi0_nubar", "NC_RES_ppi-_nubar", "NC_RES_npi+_nubar",
    "NC_multi_pi_nubar", "CC_QE_bound_proton_nubar", "NC_elastic_free_p_nubar",
    "NC_elastic_bound_p_nubar", "NC_elastic_n_nubar", "CC_COH_nubar",
    "NC_COH_nubar", "CC_eta_nubar", "NC_eta_n_nubar", "NC_eta_p_nubar",
    "CC_kaon_nubar", "NC_kaon_n_nubar", "NC_kaon_p_nubar", "CC_DIS_nubar",
    "NC_DIS_nubar", "CC_1gamma_nubar", "NC_1gamma_n_nubar", "NC_1gamma_p_nubar",
    "CC_2p2h_nubar", "CC_DIF_nubar", "NC_DIF_nubar",
)

_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"Cannot render {value!r} as a TOML value")


def render_toml(table: dict[str, Any], _prefix: tuple[str, ...] = ()) -> str:
    """Render nested dicts as TOML: scalars first, then one ``[a.b]`` per sub-dict.

    Covers exactly what the NEUT 6 card needs (strings, numbers, arrays, tables;
    keys with ``+``/``-`` quoted), so the framework needs no TOML writer package.
    """
    key = lambda k: k if _BARE_TOML_KEY.match(k) else json.dumps(k)  # noqa: E731
    lines = [f"{key(k)} = {_toml_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    for k, v in table.items():
        if isinstance(v, dict):
            path = (*_prefix, k)
            lines += ["", "[" + ".".join(key(p) for p in path) + "]", render_toml(v, path)]
    return "\n".join(lines).strip("\n")


def card_format(code_version: str) -> str:
    """``"toml"`` for NEUT 6 code versions, ``"legacy"`` (the Fortran card) otherwise."""
    from ..generators.neut import NeutAdapter  # the catalog; imported lazily (cycle)

    entry = NeutAdapter.CODE_VERSIONS.get(str(code_version)) or {}
    return str(entry.get("card_format", "legacy"))


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
        fmt = card_format(task["code_version"])
        known = CONFIG_VERSION_CARDS if fmt == "legacy" else NEUT6_CONFIG_VERSIONS
        if config_version not in known:
            raise KeyError(
                f"No NEUT card parameters defined for config_version "
                f"'{config_version}'. Known: {', '.join(known)}"
            )
        if fmt == "toml":
            card = self._toml_card(task, particle, protons, neutrons, current)
            _merge(card["neut"], NEUT6_CONFIG_VERSIONS[config_version])
        else:
            card = self._card(task, particle, protons, neutrons, config_version, current)

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
            "card_format": fmt,
            "neut_card": card,
        }

    @staticmethod
    def _toml_card(
        task: dict[str, Any],
        particle: str,
        protons: int,
        neutrons: int,
        current: str,
    ) -> dict[str, Any]:
        """NEUT 6's TOML card, as nested dicts (rendered by :func:`render_toml`).

        NEUT 6 overlays this onto ``$NEUT_CARDS/NEUT.Defaults.toml``, so only the
        framework's own settings are given here; the config version's model
        overrides (:data:`NEUT6_CONFIG_VERSIONS`) are merged in by the caller. The
        settings mirror :meth:`_card` key for key: histogram flux in GeV,
        ``RANFILE`` seed, fixed vertex and direction, pure nucleus, FSI on, and
        the weak current through per-mode scaling. The output format must be
        pinned: NEUT 6 defaults to NuHepMC, and nf_flatten.C reads NeutVect.
        """
        card: dict[str, Any] = {
            "neut": {
                "number_of_events": int(task["event_count"]),
                "rng_seed": "file",
                "output_format": "neutroot",
                "simulation_mode": "neutrino",
                "vertex": {"position_type": "origin"},
                # Same names as the framework's probes (numu, numubar, ...).
                "probe": {
                    "species": particle,
                    "direction_type": "z",
                    "energy_spectrum_type": "root",
                    "energy_histogram": [FLUX_FILE, FLUX_HIST, "gev"],
                },
                "target": {
                    "num_nucleons": protons + neutrons,
                    "num_bound_protons": protons,
                    "num_free_protons": 0,
                    "hadron_FSI": {
                        "pion_cascade": {"pion_rescattering": "on"},
                        "nucleon_cascade": {"nucleon_rescattering": "on"},
                    },
                },
                "interaction_model": {"mode_choice_type": "normal"},
            }
        }
        if current != "inclusive":
            model = card["neut"]["interaction_model"]
            model["mode_choice_type"] = "rescaled"
            model["mode_rescaling"] = {
                name: 1.0 if name.lower().startswith(current + "_") else 0.0
                for name in NEUT6_MODE_NAMES
            }
        return card

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
            # Final-state interactions -- pion (NEUT-NEFF 0 = on, despite
            # necard.h's "in O16" name) and nucleon (NUCRES-RESCAT 1 = on)
            # cascades -- which shape the post-FSI list the final-state
            # columns are read from. Both are NEUT's defaults per necard.h and
            # the shipped neut_5.4.0_* cards; pinned so the columns do not
            # depend on them.
            "NEUT-NEFF": 0,
            "NUCRES-RESCAT": 1,
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
