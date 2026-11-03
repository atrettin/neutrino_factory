from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator, physics_current
from ..flux import Flux, build_flux
from ..particles import nucleus_composition, probe_pdg

# Number of equal-width bins used to approximate a continuous spectrum as a
# NuWro `beam_energy` histogram. NuWro's parser caps at 5000 bins.
FLUX_NBINS = 500

# Cross section unit scale: raw NuWro weights are a plain cross section value
# in cm^2 (see compute_xsec_weight); this rescales to the common "1e-38 cm^2"
# convention so numbers stay O(1) near 1 GeV instead of O(1e-38).
XSEC_SCALE = 1e38

# The nuclear-target dynamics channels NuWro switches on and off individually,
# as ``dyn_<channel>_<current>`` parameters. Every one of them exists in NuWro's
# shipped data/params.txt (verified in the nuwro_25.11 image), whose own default
# is CC-only: all ``_cc`` on, all ``_nc`` off.
DYNAMICS_CHANNELS = ("qel", "res", "dis", "coh", "mec")

# Channels outside the ``<channel>_<current>`` grid, pinned explicitly so that
# what "cc"/"nc"/"inclusive" mean does not depend on NuWro's built-in defaults:
#
# * ``dyn_hyp_cc`` — quasi-elastic hyperon production, a genuine CC channel
#   (antineutrinos only), so it follows the CC switch.
# * ``dyn_lep`` — neutrino-electron scattering. It is *on* in NuWro's default
#   params.txt, mixes both currents, and is not a nuclear-target process at all:
#   its target is an atomic electron, while this framework's xsec_weight is
#   normalized per target *nucleon*. Including it would put events with an
#   incommensurable normalization into the same output, so it is switched off
#   for every current.
# * ``dyn_qel_el`` — (quasi-)elastic *electron* scattering, for electron beams;
#   irrelevant to a neutrino run and off in NuWro's defaults too.
EXTRA_DYNAMICS = {"hyp_cc": "cc", "lep": None, "qel_el": None}

# config_version -> the NuWro parameters that define it. Both pin the target
# nucleon model of the non-QE channels to NuWro's own default, the local Fermi
# gas (nucleus_target = 2), and differ only in the QE nuclear model:
#
# * "sf": the spectral function (sf_method = 1, NuWro's default). It exists only
#   for the nuclei in SF_NUCLEI (``sfevent``, src/sfevent.cc, returns no event
#   for any other), so validate_target refuses the rest.
# * "lfg": the local Fermi gas for QE too (sf_method = 0, ``qelevent1``). It
#   works on every nucleus. On H1 NuWro takes the proton at rest without binding
#   energy (``nucleus::get_nucleon`` and ``qelevent1`` for A = 1).
#
# "default" is legacy: NuWro's built-in parameters, with nothing written. That
# is the "sf" physics, but it ran on every nucleus, so its H1 and W184 samples
# have no QE events. It is kept so that its runs can still be merged; submit
# and retry refuse it (NuWroAdapter.CODE_VERSIONS).
CONFIG_VERSION_PARAMS: dict[str, dict[str, int]] = {
    "sf": {"sf_method": 1, "nucleus_target": 2},
    "lfg": {"sf_method": 0, "nucleus_target": 2},
    "default": {},
}

# The nuclei, as (protons, neutrons), that NuWro 25.11's sf_method = 1 has a
# spectral function for (``sfevent``, src/sfevent.cc).
SF_NUCLEI = {"C12": (6, 6), "O16": (8, 8), "Ar40": (18, 22), "Fe56": (26, 30)}

# config_version -> the nuclei it can predict; absent means every nucleus.
CONFIG_VERSION_NUCLEI = {"sf": SF_NUCLEI}


class NuWroTranslator(ConfigTranslator):
    name = "nuwro"

    @classmethod
    def validate_target(cls, config_version: str, nucleus: str, where: str) -> list[str]:
        """Refuse a nucleus the config version has no model for (``CONFIG_VERSION_NUCLEI``)."""
        supported = CONFIG_VERSION_NUCLEI.get(config_version)
        if supported is None:
            return []
        try:
            composition = nucleus_composition(nucleus)
        except ValueError:
            return []  # config validation reports the malformed name itself
        if composition in supported.values():
            return []
        return [
            f"{where}: NuWro config_version '{config_version}' cannot predict {nucleus}: "
            f"it is defined only for {', '.join(supported)}. Use 'lfg' for every nucleus, "
            "or drop this target."
        ]

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

        # nucleus_p / nucleus_n derived from the name, so any isotope NuWro
        # itself supports works without a table entry here.
        protons, neutrons = nucleus_composition(nucleus)
        current = physics_current(config)

        config_version = str(task["config_version"])
        if config_version not in CONFIG_VERSION_PARAMS:
            raise KeyError(
                f"No NuWro parameters defined for config_version '{config_version}'. "
                f"Known: {', '.join(CONFIG_VERSION_PARAMS)}"
            )
        problems = self.validate_target(config_version, nucleus, "NuWro task")
        if problems:
            raise ValueError(problems[0])

        # NuWro (beam_type=0) reads the spectrum straight from `beam_energy`.
        # target_type=0 selects a single nucleus via nucleus_p / nucleus_n.
        nuwro_params = {
            "number_of_events": int(task["event_count"]),
            "random_seed": seed,
            "beam_particle": probe_pdg(particle, "NuWro"),
            "beam_type": 0,
            "beam_energy": self._beam_energy(flux),
            "nucleus_p": protons,
            "nucleus_n": neutrons,
            "target_type": 0,
            **self._dynamics(current),
            # The intranuclear cascade, which fills e/post -- the post-FSI list
            # the final-state columns are read from. On in NuWro's own default
            # (params_all.h) too; pinned so the columns never silently fall
            # back to the pre-FSI copy NuWro writes into e/post when it is off.
            "FSI_on": 1,
            **CONFIG_VERSION_PARAMS[config_version],
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
            "current": current,
            "code_version": task["code_version"],
            "config_version": config_version,
            "generator_version_id": task.get("generator_version_id"),
            "nuwro_params": nuwro_params,
            # Reweight universes (optional): the validated config block, resolved
            # against the generation's own parameter values after the run (see
            # NuWroAdapter._run_reweight). params.txt is deliberately left alone,
            # so the central MC stays the config_version's tune.
            "universes": config.get("job", {}).get("nuwro", {}).get("universes"),
        }

    @staticmethod
    def _dynamics(current: str) -> dict[str, int]:
        """NuWro's ``dyn_*`` switches for the requested weak current.

        Written out in full for every current — including the channels that are
        off — so the params file states the complete dynamics set rather than
        inheriting half of it from NuWro's own defaults.

        No ``compute_xsec_weight`` change is needed to go with this: NuWro's raw
        per-event weight is ``chooser::total()``, the flux-averaged sum over the
        *active* channels, so restricting the channels rescales the weight by
        construction.
        """
        wanted = {"cc", "nc"} if current == "inclusive" else {current}
        switches = {
            f"dyn_{channel}_{channel_current}": int(channel_current in wanted)
            for channel in DYNAMICS_CHANNELS
            for channel_current in ("cc", "nc")
        }
        switches.update(
            {
                f"dyn_{name}": int(channel_current in wanted)
                for name, channel_current in EXTRA_DYNAMICS.items()
            }
        )
        return switches

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
