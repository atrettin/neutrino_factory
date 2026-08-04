from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import Flux, HistogramFlux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.neut import NeutTranslator
from .base import OutputNormalizer

# NEUT interaction mode -> common-output interaction category, keyed on the
# absolute mode (NEUT negates the mode for antineutrinos). Spelled out rather
# than expressed as ranges so a mode NEUT adds later falls through to "other"
# instead of being silently absorbed into a neighbouring category.
#
# NCEL (51/52) is grouped with CCQE under "qel" to match GENIE, whose gst "qel"
# flag likewise covers both.
INTERACTION_BY_MODE: dict[int, str] = {
    1: "qel",   # CC quasi-elastic
    51: "qel",  # NC elastic (proton)
    52: "qel",  # NC elastic (neutron)
    2: "mec",   # CC 2p2h / MEC
    11: "res", 12: "res", 13: "res",              # CC resonant 1pi
    17: "res",                                    # CC 1gamma
    22: "res", 23: "res",                         # CC 1eta, CC 1K
    31: "res", 32: "res", 33: "res", 34: "res",   # NC resonant 1pi
    38: "res", 39: "res",                         # NC 1gamma
    42: "res", 43: "res",                         # NC 1eta
    44: "res", 45: "res",                         # NC 1K
    21: "dis", 26: "dis",                         # CC multi-pi, CC DIS
    41: "dis", 46: "dis",                         # NC multi-pi, NC DIS
    16: "coh", 36: "coh",                         # CC / NC coherent pi
}

# The weak current is encoded in the mode number itself: NEUT numbers charged
# current channels 1..30 and neutral current channels 31 and up (the mode is
# negated for antineutrinos, so compare on the absolute value). The
# INTERACTION_BY_MODE comments above are the per-mode statement of the same rule.
MAX_CC_MODE = 30


def _is_cc_mode(mode: int) -> bool:
    return abs(int(mode)) <= MAX_CC_MODE


# Names of the histogram pair NEUT writes into its output when sampling a flux
# histogram; nf_flatten.C carries every histogram across verbatim. Their integral
# ratio is the flux-averaged total cross section in 1e-38 cm^2 per nucleon.
#
# The primary names carry the beam flavour: neutroot2 formats them as "flux_%s" /
# "evtrt_%s" with its own short token — "numu", "numub", "nue", "nueb" (read off
# the NEUT 5.7.0 binary; note "numub", not "numubar"). Alongside them it writes a
# generic "fluxhisto" / "ratehisto" copy, which is the only pair present for a
# beam it has no token for. Matching on the prefix covers every flavour without
# encoding NEUT's spelling, which is not a documented contract.
FLUX_HIST_PREFIX = "flux_"
FLUX_HIST_FALLBACK = "fluxhisto"
EVENT_RATE_HIST_PREFIX = "evtrt_"
EVENT_RATE_HIST_FALLBACK = "ratehisto"


def _find_histogram(handle, prefix: str, fallback: str, root_path: Path) -> str:
    """Name of the histogram in ``handle`` carrying the flux or the event rate.

    The flavour-named histogram wins; ``fallback`` is NEUT's generic copy of the
    same spectrum, used when the beam has no flavour token. Raises if neither is
    there — NEUT writes the pair only when it samples a flux histogram
    (``EVCT-MPV 3``), and without it the run cannot be normalized — or if
    several flavour-named ones are, which would leave the choice of
    normalization to a guess.
    """
    names = sorted({key.split(";")[0] for key in handle.keys()})
    matches = [name for name in names if name.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches and fallback in names:
        return fallback

    present = ", ".join(names) or "nothing"
    if not matches:
        raise RuntimeError(
            f"No histogram named {prefix}* or {fallback} in {root_path} "
            f"(found: {present}). NEUT writes it only when sampling a flux "
            "histogram (EVCT-MPV 3); without it the run cannot be normalized."
        )
    raise RuntimeError(
        f"Several histograms in {root_path} match {prefix}*: "
        f"{', '.join(matches)}. Exactly one flux/event-rate pair is expected; "
        "which one carries the normalization cannot be guessed."
    )


class NeutNormalizer(OutputNormalizer):
    name = "neut"

    def normalize(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        path = Path(raw_output_path)
        if path.suffix == ".root":
            return self._normalize_root(path, normalized_output_path, task, execution_mode)
        return self._normalize_json(path, normalized_output_path, task, execution_mode)

    def _normalize_json(self, path: Path, out_path, task: dict, mode: str) -> str:
        raw = json.loads(path.read_text(encoding="utf-8"))
        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = raw.get("translated_config", {})
        return write_common_hdf5(out_path, metadata, raw.get("events", []))

    def _normalize_root(self, root_path: Path, out_path, task: dict, mode: str) -> str:
        try:
            import uproot
        except ImportError as exc:
            raise RuntimeError(
                "uproot is required to read NEUT ROOT output. "
                "Install it with: pip install uproot"
            ) from exc

        sidecar = root_path.parent / "translated_config.json"
        if not sidecar.exists():
            raise RuntimeError(
                f"translated_config.json not found alongside {root_path}. "
                "Re-run with the current NeutAdapter to generate it."
            )
        translated = json.loads(sidecar.read_text(encoding="utf-8"))

        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = translated

        probe = translated["probe"]
        target = translated["target"]
        start_event = int(task["start_event"])

        with uproot.open(root_path) as f:
            try:
                tree = f["nf_neut"]
            except Exception as exc:
                raise RuntimeError(
                    f"No 'nf_neut' tree in {root_path}. NEUT's native NeutVect output "
                    f"must be passed through nf-neut-flatten first: {exc}"
                ) from exc
            try:
                energies_gev = tree["enu_gev"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read neutrino energy from branch 'enu_gev': {exc}"
                ) from exc
            try:
                modes = tree["mode"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read interaction mode from branch 'mode': {exc}"
                ) from exc
            try:
                # nf_flatten.C writes both four-vectors already in GeV. pdglep is
                # 0 when it found no outgoing lepton, which blanks the kinematics.
                nu_p4 = np.column_stack([
                    energies_gev,
                    *(tree[b].array(library="np") for b in ("nu_px_gev", "nu_py_gev", "nu_pz_gev")),
                ])
                lepton_p4 = np.column_stack([
                    tree[b].array(library="np")
                    for b in ("lep_e_gev", "lep_px_gev", "lep_py_gev", "lep_pz_gev")
                ])
                lepton_pdg = tree["pdglep"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    "Cannot read lepton four-vectors from 'nu_p*_gev' / 'lep_*_gev'. "
                    "The flattened file predates these branches; regenerate it with "
                    f"the current setup/neut/nf_flatten.C: {exc}"
                ) from exc
            flux_hist = _find_histogram(
                f, FLUX_HIST_PREFIX, FLUX_HIST_FALLBACK, root_path
            )
            rate_hist = _find_histogram(
                f, EVENT_RATE_HIST_PREFIX, EVENT_RATE_HIST_FALLBACK, root_path
            )
            flux_averaged_xsec = self._flux_averaged_xsec(
                f, root_path, flux_hist, rate_hist
            )

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        # NEUT is unweighted: there is no per-event raw weight to preserve, so the
        # common output's `weight` column is 1.0 and all of the cross-section
        # information lives in `xsec_weight`.
        weights = np.ones_like(energies_gev)

        translated_with_xsec = dict(translated)
        translated_with_xsec["flux_averaged_xsec_1e38"] = flux_averaged_xsec

        flux = self._generated_flux(root_path, translated, flux_hist)
        xsec_weights = NeutTranslator().compute_xsec_weight(
            energies_gev, weights, translated_with_xsec, flux
        )

        interactions = [
            INTERACTION_BY_MODE.get(abs(int(neut_mode)), "other") for neut_mode in modes
        ]
        kinematics = derive_kinematics(
            nu_p4, lepton_p4, interactions, valid=np.asarray(lepton_pdg) != 0
        )

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = NeutTranslator().xsec_norm_count(
            translated_with_xsec, len(energies_gev)
        )

        events = []
        for i, (e_gev, w, xw, itype, neut_mode) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions, modes)
        ):
            event = {
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "is_cc": _is_cc_mode(neut_mode),
                "interaction": itype,
                "probe": probe,
                "target": target,
                "generator": self.name,
            }
            event.update({field: float(kinematics[field][i]) for field in KINEMATIC_FIELDS})
            events.append(event)

        return write_common_hdf5(out_path, metadata, events)

    @staticmethod
    def _generated_flux(root_path: Path, translated: dict, flux_hist: str) -> Flux:
        """Load the spectrum NEUT actually sampled from.

        NEUT copies the TH1 it was handed into its own output as
        ``flux_<flavour>`` (edges and contents verbatim, verified against NEUT
        5.7.0; ``flux_hist`` is the name :func:`_find_histogram` resolved), and
        nf_flatten.C carries it into the flattened file. That histogram — not the
        run config — is the authoritative generated flux: NEUT draws a bin from
        it in proportion to the raw bin content and then an energy uniformly
        within that bin, so the generated flux density is piecewise constant on
        exactly these edges. Rebuilding the flux from the config would divide the
        events by a grid they were never drawn from (see the flux section of
        docs/generators/neut.md, and docs/generators/genie.md for the same
        failure mode found first there).

        ``NeutAdapter._write_flux_file`` writes per-bin integrals, hence
        ``contents_are_counts``: dividing by the bin widths recovers the density.

        A missing histogram is already a hard error in
        :meth:`_flux_averaged_xsec`; there is deliberately no fallback to the
        configured flux, which would silently restore the bug and still produce
        physical-looking numbers (CLAUDE.md, development posture).
        """
        particle = str(translated["probe"])
        return HistogramFlux.from_root_file(
            root_path, flux_hist, particle, contents_are_counts=True
        )

    @staticmethod
    def _flux_averaged_xsec(
        handle, root_path: Path, flux_hist: str, rate_hist: str
    ) -> float:
        """Flux-averaged total cross section, in 1e-38 cm^2 per target nucleon.

        NEUT writes the flux histogram it sampled and the corresponding event
        rate (flux x sigma) into its output; the ratio of their integrals is the
        flux-averaged total cross section. This is the only normalization NEUT
        emits, and it is the sole input to
        ``NeutTranslator.compute_xsec_weight`` — see that method for the checks
        establishing the units and the per-nucleon convention.
        """
        flux_contents, flux_edges = handle[flux_hist].to_numpy()
        rate_contents, rate_edges = handle[rate_hist].to_numpy()

        if not np.array_equal(flux_edges, rate_edges):
            raise RuntimeError(
                f"'{flux_hist}' and '{rate_hist}' in {root_path} have different "
                "binning; their integral ratio would not be a flux average."
            )

        flux_integral = float(np.sum(flux_contents))
        if flux_integral <= 0.0:
            raise RuntimeError(
                f"'{flux_hist}' in {root_path} integrates to {flux_integral}; cannot "
                "compute a flux-averaged cross section."
            )
        # No bin-width factor is needed, on any binning: NEUT copies the input
        # histogram verbatim, and NeutAdapter._write_flux_file writes per-bin
        # integrals (flux density x bin width). evtrt_b is then flux_b * sigma_b
        # with the width already folded in, so this ratio is
        # sum(phi_b w_b sigma_b) / sum(phi_b w_b) — the flux-weighted average.
        # Were the contents densities instead, this would only be correct for
        # equal-width bins.
        return float(np.sum(rate_contents)) / flux_integral
