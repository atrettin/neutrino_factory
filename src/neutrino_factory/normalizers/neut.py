from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
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

# Histogram pair NEUT writes into its output when sampling a flux histogram;
# nf_flatten.C carries them into the flattened file. Their integral ratio is the
# flux-averaged total cross section in 1e-38 cm^2 per nucleon.
FLUX_HIST = "flux_numu"
EVENT_RATE_HIST = "evtrt_numu"


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
            flux_averaged_xsec = self._flux_averaged_xsec(f, root_path)

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        # NEUT is unweighted: there is no per-event raw weight to preserve, so the
        # common output's `weight` column is 1.0 and all of the cross-section
        # information lives in `xsec_weight`.
        weights = np.ones_like(energies_gev)

        translated_with_xsec = dict(translated)
        translated_with_xsec["flux_averaged_xsec_1e38"] = flux_averaged_xsec

        flux = build_flux(translated["flux_config"])
        xsec_weights = NeutTranslator().compute_xsec_weight(
            energies_gev, weights, translated_with_xsec, flux
        )

        events = []
        for i, (e_gev, w, xw, neut_mode) in enumerate(
            zip(energies_gev, weights, xsec_weights, modes)
        ):
            events.append({
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "interaction": INTERACTION_BY_MODE.get(abs(int(neut_mode)), "other"),
                "probe": probe,
                "target": target,
                "generator": self.name,
            })

        return write_common_hdf5(out_path, metadata, events)

    @staticmethod
    def _flux_averaged_xsec(handle, root_path: Path) -> float:
        """Flux-averaged total cross section, in 1e-38 cm^2 per target nucleon.

        NEUT writes the flux histogram it sampled and the corresponding event
        rate (flux x sigma) into its output; the ratio of their integrals is the
        flux-averaged total cross section. This is the only normalization NEUT
        emits, and it is the sole input to
        ``NeutTranslator.compute_xsec_weight`` — see that method for the checks
        establishing the units and the per-nucleon convention.
        """
        for name in (FLUX_HIST, EVENT_RATE_HIST):
            if name not in handle:
                raise RuntimeError(
                    f"Histogram '{name}' not found in {root_path}. NEUT writes it "
                    "only when sampling a flux histogram (EVCT-MPV 3); without it "
                    "the run cannot be normalized."
                )
        flux_contents, flux_edges = handle[FLUX_HIST].to_numpy()
        rate_contents, rate_edges = handle[EVENT_RATE_HIST].to_numpy()

        if not np.array_equal(flux_edges, rate_edges):
            raise RuntimeError(
                f"'{FLUX_HIST}' and '{EVENT_RATE_HIST}' in {root_path} have different "
                "binning; their integral ratio would not be a flux average."
            )

        flux_integral = float(np.sum(flux_contents))
        if flux_integral <= 0.0:
            raise RuntimeError(
                f"'{FLUX_HIST}' in {root_path} integrates to {flux_integral}; cannot "
                "compute a flux-averaged cross section."
            )
        # Bin widths cancel in the ratio: NEUT copies the input histogram's
        # binning, and the adapter writes that from Flux.to_histogram, whose bins
        # are equal-width.
        return float(np.sum(rate_contents)) / flux_integral
