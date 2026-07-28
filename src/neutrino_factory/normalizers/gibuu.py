from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.gibuu import GiBUUTranslator
from .base import OutputNormalizer


def _interaction_from_evtype(ev_type: int) -> str:
    """Map GiBUU's ``evType`` event-class code to the common interaction label.

    GiBUU's neutrino event classification (see the ``EventInfo``/``K2Hist``
    convention): 1 = QE, 2..31 = resonances (2 = Delta), 32/33 = non-resonant
    1-pion background, 34 = DIS, 35/36 = 2p2h (MEC). Everything else is bucketed
    as ``other``. Confirmed against a real release2025 numu-CC carbon run.
    """
    if ev_type == 1:
        return "qel"
    if 2 <= ev_type <= 31:
        return "res"
    if ev_type == 34:
        return "dis"
    if ev_type in (35, 36):
        return "mec"
    return "other"


class GiBUUNormalizer(OutputNormalizer):
    name = "gibuu"

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
                "uproot is required to read GiBUU ROOT output. "
                "Install it with: pip install uproot"
            ) from exc

        sidecar = root_path.parent / "translated_config.json"
        if not sidecar.exists():
            raise RuntimeError(
                f"translated_config.json not found alongside {root_path}. "
                "Re-run with the current GiBUUAdapter to generate it."
            )
        translated = json.loads(sidecar.read_text(encoding="utf-8"))

        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = translated

        probe = translated["beam_particle"]
        target = translated["nucleus"]
        start_event = int(task["start_event"])

        with uproot.open(root_path) as f:
            tree = f["RootTuple"]
            try:
                # GiBUU writes lepIn_E in GeV; it is the incoming neutrino energy.
                energies_gev = tree["lepIn_E"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read neutrino energy from branch 'lepIn_E': {exc}"
                ) from exc
            try:
                weights = tree["weight"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read event weight from branch 'weight': {exc}"
                ) from exc
            try:
                ev_types = tree["evType"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read interaction class from branch 'evType': {exc}"
                ) from exc
            try:
                # Both four-vectors are in GeV: lepIn_* is the incoming neutrino,
                # lepOut_* the outgoing lepton (the scattered neutrino for NC).
                nu_p4 = np.column_stack([
                    energies_gev,
                    *(tree[branch].array(library="np") for branch in ("lepIn_Px", "lepIn_Py", "lepIn_Pz")),
                ])
                lepton_p4 = np.column_stack([
                    tree[branch].array(library="np")
                    for branch in ("lepOut_E", "lepOut_Px", "lepOut_Py", "lepOut_Pz")
                ])
            except Exception as exc:
                raise RuntimeError(
                    "Cannot read lepton four-vectors from 'lepIn_P*' and 'lepOut_*': "
                    f"{exc}"
                ) from exc

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = GiBUUTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        interactions = [_interaction_from_evtype(int(ev_type)) for ev_type in ev_types]
        kinematics = derive_kinematics(nu_p4, lepton_p4, interactions)

        events = []
        for i, (e_gev, w, xw, itype) in enumerate(
            zip(energies_gev, weights_arr, xsec_weights, interactions)
        ):
            event = {
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "interaction": itype,
                "probe": probe,
                "target": target,
                "generator": self.name,
            }
            event.update({field: float(kinematics[field][i]) for field in KINEMATIC_FIELDS})
            events.append(event)

        return write_common_hdf5(out_path, metadata, events)
