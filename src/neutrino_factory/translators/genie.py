from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator
from ..flux import Flux, HistogramFlux, PowerLawFlux, build_flux

# PDG codes for gevgen's -p (probe) flag. gevgen expects a numeric PDG code,
# not a flavour name. Mirrors the mapping used by the NuWro translator.
PARTICLE_PDG = {
    "numu": 14,
    "nue": 12,
    "numubar": -14,
    "nuebar": -12,
    "nutau": 16,
    "nutaubar": -16,
}

# Number of grid points used to flux-average the reconstructed total cross
# section (see compute_xsec_weight). Mirrors NuWro's FLUX_NBINS.
FLUX_NBINS = 500

# Cross section unit scale: matches XSEC_SCALE in translators/nuwro.py, the
# shared "1e-38 cm^2" convention for the common output's xsec_weight column.
XSEC_SCALE = 1e38

# GENIE's internal unit system is natural units with GeV=1 (see
# Framework/Conventions/Units.h): meter = 1/(hbarc*GeV), cm = 0.01*meter,
# cm2 = cm*cm. This is the same conversion gNtpConv.cxx applies
# (`brXSec = event.XSec()*(1E+38/units::cm2)`) to express a raw internal xsec
# value in "1e-38 cm^2". hbarc = 1.973269804e-16 GeV*m (exact, CODATA).
_HBARC_GEV_M = 1.973269804e-16
_GENIE_METER = 1.0 / _HBARC_GEV_M
_GENIE_CM = 0.01 * _GENIE_METER
GENIE_UNITS_CM2 = _GENIE_CM * _GENIE_CM

# Matches a <spline name="..."> opening tag, capturing the name attribute.
_SPLINE_OPEN_RE = re.compile(r'<spline\s+name="([^"]*)"')
# Matches a single <knot><E>..</E><xsec>..</xsec></knot> entry.
_KNOT_RE = re.compile(
    r"<knot>\s*<E>\s*([-+0-9.eE]+)\s*</E>\s*<xsec>\s*([-+0-9.eE]+)\s*</xsec>\s*</knot>"
)


class GenieTranslator(ConfigTranslator):
    name = "genie"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        if particle not in PARTICLE_PDG:
            raise KeyError(
                f"Unknown neutrino particle '{particle}' for GENIE probe. "
                f"Known: {', '.join(PARTICLE_PDG)}"
            )

        return {
            "generator": self.name,
            "command": "gevgen",
            "probe": particle,
            "probe_pdg": PARTICLE_PDG[particle],
            "target": target["nucleus"],
            "target_pdg": target.get("pdg", target["nucleus"]),
            "energy_range_gev": [flux.emin_gev, flux.emax_gev],
            "events": int(task["event_count"]),
            "seed": int(task["seed"]),
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "genie_flux": self._genie_flux_descriptor(flux),
            "event_generator_list": config["physics"].get("event_generator_list"),
            "physics_mode": config["physics"].get("mode", "inclusive"),
            # For GENIE, config_version is the tune and code_version is the git tag.
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
        }

    @staticmethod
    def _genie_flux_descriptor(flux: Any) -> dict[str, Any]:
        """Translate the framework flux into gevgen's -f argument spec.

        Power law -> a ROOT TF1 function string ``x^(gamma)`` where ``x`` is the
        neutrino energy in GeV. Histogram -> the ROOT file + TH1 name, consumed
        directly by gevgen's TH1 flux driver.
        """
        if isinstance(flux, PowerLawFlux):
            return {"kind": "function", "expr": f"x^({flux.gamma})"}
        if isinstance(flux, HistogramFlux):
            # HistogramFlux was built from a ROOT file; carry the source so the
            # adapter can hand the same file to gevgen. build_flux resolved the
            # path to absolute already.
            return {
                "kind": "histogram",
                "file": str(flux.source_path),
                "name": flux.source_name,
            }
        raise TypeError(f"Unsupported flux type for GENIE: {type(flux).__name__}")

    def compute_xsec_weight(
        self,
        energies_gev: np.ndarray,
        raw_weights: np.ndarray,
        translated_config: dict[str, Any],
        flux: Flux,
    ) -> np.ndarray:
        """Recover the physical, energy-resolved cross section from GENIE output.

        Unlike NuWro, GENIE's ``gevgen`` generates *unweighted* events by
        default (``gOptWeighted = false`` in ``Apps/gEvGen.cxx``), so the gst
        tree's ``wght`` branch (``raw_weights`` here) is always 1.0 and carries
        no normalization information; it is accepted for interface parity with
        :class:`ConfigTranslator` but unused below. The gst ``XSec``/``DXSec``
        branches are not usable either: both trace back to
        ``EventRecord::XSec()``, set in
        ``Framework/EventGen/PhysInteractionSelector.cxx`` (``SelectInteraction``)
        to the cross section of only the *one selected channel* at that event's
        kinematics, never the summed total over all channels/nucleons that
        actually governed accept/reject (``xsec_sum`` in that same function,
        never persisted to any output). Averaging a per-channel value over
        events does not converge to the total - channel selection is itself
        correlated with that channel's own cross section.

        However, GENIE's rejection sampling is physically identical to
        NuWro's: ``PhysInteractionSelector`` sums every channel's cross section
        for the target into ``xsec_sum``, and
        ``GMCJDriver::ComputeInteractionProbabilities``
        (``ForceSingleProbScale`` makes this an absolute, physical probability
        in unweighted mode) accepts/rejects the flux neutrino proportional to
        it. So accepted events still land in energy with density proportional
        to flux(E) * sigma_total(E), exactly as documented in
        ``NuWroTranslator.compute_xsec_weight`` - only the run's flux-averaged
        total cross section constant ``C`` needs to come from elsewhere, since
        GENIE (unlike NuWro) does not stamp it onto every event.

        ``C`` is reconstructed from the cross-section spline file already
        staged for this run (``--cross-sections``, required for any flux-driven
        gevgen run - see ``GenieAdapter.genie_xsecs_xml``). Summing every
        ``<spline>`` in that file whose name matches
        ``nu:<probe_pdg>;tgt:<target_pdg>;`` (regardless of struck-nucleon or
        process) reproduces the same ``xsec_sum`` GENIE computes internally for
        that initial state - verified against the real staged file for
        R-3_06_00/G18_10a_02_11a (103 matching splines for numu/Ar40, covering
        QEL/RES/DIS/COH/MEC including the dummy 2p2h pair codes).

        Unit note: raw ``<xsec>`` knot values are in GENIE's internal natural
        units (GeV-based), not literal cm^2 - ``GENIE_UNITS_CM2`` converts them
        the same way ``gNtpConv.cxx`` does for its own ROOT branches.

        Per-nucleon note: the spline ``tgt:`` tag is the *compound nucleus* PDG
        code (e.g. 1000180400 for the whole Ar40 nucleus), so the reconstructed
        total is already a whole-nucleus quantity - the opposite of NuWro,
        whose raw per-event weight is already per-nucleon. Dividing by the
        nucleus's mass number (read directly off the target PDG code) converts
        to the shared "per nucleon" output convention.
        """
        xml_path = self._resolve_xsecs_xml(translated_config)
        probe_pdg = int(translated_config["probe_pdg"])
        target_pdg = int(translated_config["target_pdg"])
        mass_number = (target_pdg // 10) % 1000

        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        widths = np.diff(edges)
        centers = 0.5 * (edges[:-1] + edges[1:])
        clipped = np.clip(contents, 0.0, None)
        integral = float(np.sum(clipped * widths))

        xsec_weight = np.zeros_like(np.asarray(energies_gev, dtype=np.float64))
        if integral <= 0.0:
            return xsec_weight

        sigma_internal_sum = self._sum_matching_splines(xml_path, probe_pdg, target_pdg, centers)
        if sigma_internal_sum is None:
            return xsec_weight

        sigma_per_nucleon = sigma_internal_sum * (XSEC_SCALE / GENIE_UNITS_CM2) / mass_number

        flux_averaged_xsec = float(np.sum(clipped * widths * sigma_per_nucleon)) / integral

        n_events = len(energies_gev)
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

        GENIE is unweighted/rejection-sampled, so each event is one sample of the
        same estimator and a chunk's statistical size is simply how many events
        it holds.
        """
        return float(event_count)

    @staticmethod
    def _resolve_xsecs_xml(translated_config: dict[str, Any]) -> Path:
        from ..generators.genie import GenieAdapter

        code_version = str(translated_config.get("code_version") or "")
        tune = str(translated_config.get("config_version") or "")
        software_root = os.environ.get("NF_SOFTWARE_ROOT", "./software")
        xml_path = GenieAdapter.genie_xsecs_xml(software_root, code_version, tune)
        if xml_path is None:
            raise RuntimeError(
                f"No staged GENIE cross-section spline found for code_version "
                f"'{code_version}', tune '{tune}' under {software_root}. It is "
                "required to reconstruct xsec_weight; stage it with "
                "setup/download_genie_xsec.sh."
            )
        return xml_path

    @staticmethod
    def _sum_matching_splines(
        xml_path: Path, probe_pdg: int, target_pdg: int, query_energies_gev: np.ndarray
    ) -> np.ndarray | None:
        """Sum every spline for ``nu:<probe_pdg>;tgt:<target_pdg>;`` at each query energy.

        Streams the (potentially huge) spline XML line by line rather than
        parsing it as a DOM, since only a small fraction of its splines match a
        given (probe, target) pair.
        """
        needle = f"nu:{probe_pdg};tgt:{target_pdg};"
        total = np.zeros_like(query_energies_gev, dtype=np.float64)
        matched = False

        in_match = False
        knot_e: list[float] = []
        knot_x: list[float] = []

        with open(xml_path, "r", encoding="ISO-8859-1") as handle:
            for line in handle:
                if not in_match:
                    open_match = _SPLINE_OPEN_RE.search(line)
                    if open_match and needle in open_match.group(1):
                        in_match = True
                        knot_e = []
                        knot_x = []
                    continue

                if "</spline>" in line:
                    if len(knot_e) >= 2:
                        total += np.interp(
                            query_energies_gev, knot_e, knot_x, left=0.0, right=knot_x[-1]
                        )
                        matched = True
                    in_match = False
                    continue

                knot_match = _KNOT_RE.search(line)
                if knot_match:
                    knot_e.append(float(knot_match.group(1)))
                    knot_x.append(float(knot_match.group(2)))

        return total if matched else None
