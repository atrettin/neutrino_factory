from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator, physics_current
from ..flux import Flux, HistogramFlux, PowerLawFlux, build_flux
from ..particles import probe_pdg

# Fallback number of grid points used to flux-average the reconstructed total
# cross section (see compute_xsec_weight) when the flux has no native binning.
# Mirrors NuWro's FLUX_NBINS.
FLUX_NBINS = 500

# Binning of the flux histogram handed to gevgen for a power-law flux. gevgen
# never samples a continuous function: given a TF1 string it builds a 300-bin
# uniform TH1D and *Monte-Carlo fills it* with only 100k entries
# (Apps/gEvGen.cxx, TH1FluxDriver, the `else` branch), so the generated spectrum
# is both coarse and Poisson-noisy at high energy. Handing gevgen a ROOT file
# instead takes the branch that clones our histogram verbatim. Log spacing gives
# constant relative resolution (~0.27%/bin over 0.1-50 GeV at 1000 bins), which
# a uniform binning cannot deliver for a steeply falling spectrum.
GENIE_FLUX_NBINS = 1000
GENIE_FLUX_SPACING = "log"
# TH1 name inside the flux file we write, and the file's basename in the work dir.
GENIE_FLUX_HIST = "nf_flux"
GENIE_FLUX_FILE = "nf_flux.root"

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

# physics.current -> gevgen's --event-generator-list. The "CC" and "NC" lists are
# defined in $GENIE/config/EventGeneratorListAssembler.xml (verified in the
# R-3_06_00 image: CC covers QEL/RES/DIS/COH/MEC/DFR plus the charm and Lambda
# channels, NC the corresponding six). There is no "Default" param_set in that
# file — gevgen's own default already runs both currents — so "inclusive" omits
# the option entirely rather than naming a list that does not exist.
EVENT_GENERATOR_LISTS = {"cc": "CC", "nc": "NC", "inclusive": None}

# The `proc:` tag a spline name carries for each current, used to restrict the
# reconstructed total cross section to the channels gevgen was actually allowed
# to generate (see compute_xsec_weight).
SPLINE_PROCESS_TAGS = {"cc": "proc:Weak[CC]", "nc": "proc:Weak[NC]"}

# Matches a <spline name="..."> opening tag, capturing the name attribute.
_SPLINE_OPEN_RE = re.compile(r'<spline\s+name="([^"]*)"')
# Matches a single <knot><E>..</E><xsec>..</xsec></knot> entry.
_KNOT_RE = re.compile(
    r"<knot>\s*<E>\s*([-+0-9.eE]+)\s*</E>\s*<xsec>\s*([-+0-9.eE]+)\s*</xsec>\s*</knot>"
)


def _flux_grid(flux: Flux) -> tuple[np.ndarray, np.ndarray]:
    """Return the ``(bin_edges, bin_contents)`` grid to divide the flux out on.

    For a :class:`HistogramFlux` this is the histogram's *native* binning. That
    matters for correctness, not just accuracy: GENIE samples energies uniformly
    within the bins of the histogram it was given
    (``GCylindTH1Flux::GenerateNext`` -> ``TH1::GetRandom``), so the generated
    flux density is piecewise constant on exactly those edges. Re-binning it onto
    any other grid divides the events by a flux they were never drawn from and
    imprints a sawtooth beating the two binnings against each other.

    Only a flux with no native binning falls back to a resampled grid.
    """
    if isinstance(flux, HistogramFlux):
        return flux.bin_edges, flux.bin_contents
    return flux.to_histogram(nbins=FLUX_NBINS)


class GenieTranslator(ConfigTranslator):
    name = "genie"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        particle_pdg = probe_pdg(particle, "GENIE")
        current = physics_current(config)

        return {
            "generator": self.name,
            "command": "gevgen",
            "probe": particle,
            "probe_pdg": particle_pdg,
            "target": target["nucleus"],
            "target_pdg": target.get("pdg", target["nucleus"]),
            "energy_range_gev": [flux.emin_gev, flux.emax_gev],
            "events": int(task["event_count"]),
            "seed": int(task["seed"]),
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "genie_flux": self._genie_flux_descriptor(flux),
            "current": current,
            "event_generator_list": EVENT_GENERATOR_LISTS[current],
            "physics_mode": config["physics"].get("mode", "inclusive"),
            "log_level": config["run"].get("log_level", "default"),
            # For GENIE, config_version is the tune and code_version is the git tag.
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            # Resolved here (same process and cwd as generation) so the sidecar's
            # spline root does not depend on where normalization happens to run.
            "software_root": str(
                Path(config["storage"]["software_root"]).expanduser().resolve()
            ),
            "generator_version_id": task.get("generator_version_id"),
        }

    @staticmethod
    def _genie_flux_descriptor(flux: Any) -> dict[str, Any]:
        """Translate the framework flux into gevgen's -f argument spec.

        Power law -> a log-binned TH1 that the adapter materializes into the work
        directory (``GENIE_FLUX_FILE``) and hands to gevgen's TH1 flux driver. We
        deliberately do *not* pass a TF1 function string: gevgen would resample it
        into a coarse, Poisson-noisy 300-bin histogram (see ``GENIE_FLUX_NBINS``).
        Histogram -> the ROOT file + TH1 name, consumed directly by the same driver.
        """
        if isinstance(flux, PowerLawFlux):
            return {
                "kind": "generated_histogram",
                "file": GENIE_FLUX_FILE,
                "name": GENIE_FLUX_HIST,
                "nbins": GENIE_FLUX_NBINS,
                "spacing": GENIE_FLUX_SPACING,
            }
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

        **The sum must cover exactly the channels gevgen was allowed to
        generate.** For ``physics.current: cc`` or ``nc`` the run is restricted
        with ``--event-generator-list``, and ``xsec_sum`` is then the sum over
        that current alone; the spline sum is therefore filtered on the matching
        ``proc:Weak[CC]``/``proc:Weak[NC]`` tag in the spline name. Leaving the
        filter out would fold the other current's cross section into ``C`` and
        overstate the result (for numu on carbon, by roughly a third).

        Flux note: ``flux`` must be the spectrum GENIE *actually sampled*, i.e.
        the ``spectrum`` histogram gevgen writes to ``input-flux.root``, not the
        flux from the run config. gevgen never draws from a continuous function:
        ``TH1FluxDriver`` always ends up with a TH1D, and ``GCylindTH1Flux``
        draws from it with ``TH1::GetRandom``, which is uniform within a bin. The
        generated flux density is therefore piecewise constant, and it may differ
        from the configured flux in ways only the file records (bins outside
        ``-e`` are zeroed; a TF1 input is Monte-Carlo resampled with 100k
        entries). ``_flux_grid`` keeps that native binning intact.

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
        probe_pdg_code = int(translated_config["probe_pdg"])
        target_pdg = int(translated_config["target_pdg"])
        mass_number = (target_pdg // 10) % 1000

        edges, contents = _flux_grid(flux)
        widths = np.diff(edges)
        centers = 0.5 * (edges[:-1] + edges[1:])
        clipped = np.clip(contents, 0.0, None)
        integral = float(np.sum(clipped * widths))

        xsec_weight = np.zeros_like(np.asarray(energies_gev, dtype=np.float64))
        if integral <= 0.0:
            return xsec_weight

        current = str(translated_config.get("current", "cc")).lower()
        sigma_internal_sum = self._sum_matching_splines(
            xml_path, probe_pdg_code, target_pdg, centers, SPLINE_PROCESS_TAGS.get(current)
        )
        if sigma_internal_sum is None:
            # No spline for this beam/target: the run cannot be normalized at
            # all. Returning the zero-filled array instead would hand downstream
            # analyses physical-looking events whose every cross-section weight
            # is silently zero. gxspl-NUsmall.xml carries nue/nuebar/numu/numubar
            # only, so a nutau run lands here.
            raise RuntimeError(
                f"No cross-section spline for probe PDG {probe_pdg_code} on target PDG "
                f"{target_pdg} (current '{current}') in {xml_path}. xsec_weight "
                "cannot be reconstructed; stage a spline set covering this probe "
                "with setup/download_genie_xsec.sh, or generate a probe the "
                "staged tune supports."
            )

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
        # The root must be the one generation used (storage.software_root, carried
        # in the sidecar) - falling back to NF_SOFTWARE_ROOT could silently point
        # at a different tune's splines and produce a wrong xsec_weight.
        software_root = translated_config.get("software_root")
        if not software_root:
            raise RuntimeError(
                "translated_config carries no 'software_root'; it is required to "
                "locate the GENIE cross-section splines for xsec_weight. Re-run "
                "generation with the current GenieTranslator."
            )
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
        xml_path: Path,
        probe_pdg: int,
        target_pdg: int,
        query_energies_gev: np.ndarray,
        process_tag: str | None = None,
    ) -> np.ndarray | None:
        """Sum every spline for ``nu:<probe_pdg>;tgt:<target_pdg>;`` at each query energy.

        Streams the (potentially huge) spline XML line by line rather than
        parsing it as a DOM, since only a small fraction of its splines match a
        given (probe, target) pair.

        ``process_tag`` (``proc:Weak[CC]`` / ``proc:Weak[NC]``) additionally
        restricts the sum to one weak current; ``None`` sums both, which is what
        an inclusive run generates.
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
                    if (
                        open_match
                        and needle in open_match.group(1)
                        and (process_tag is None or process_tag in open_match.group(1))
                    ):
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
