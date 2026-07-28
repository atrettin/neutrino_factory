from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.genie import GenieTranslator
from .base import OutputNormalizer, interaction_from_flags


class GenieNormalizer(OutputNormalizer):
    name = "genie"

    def normalize(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        path = Path(raw_output_path)
        if path.suffix == ".root":
            return self._normalize_gst_root(path, normalized_output_path, task, execution_mode)
        return self._normalize_json(path, normalized_output_path, task, execution_mode)

    def _normalize_json(self, path: Path, out_path, task: dict, mode: str) -> str:
        raw = json.loads(path.read_text(encoding="utf-8"))
        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = raw.get("translated_config", {})
        return write_common_hdf5(out_path, metadata, raw.get("events", []))

    def _normalize_gst_root(self, root_path: Path, out_path, task: dict, mode: str) -> str:
        try:
            import uproot
        except ImportError as exc:
            raise RuntimeError(
                "uproot is required to read GENIE ROOT output. "
                "Install it with: pip install uproot"
            ) from exc

        sidecar = root_path.parent / "translated_config.json"
        if not sidecar.exists():
            raise RuntimeError(
                f"translated_config.json not found alongside {root_path}. "
                "Re-run with the current GenieAdapter to generate it."
            )
        translated = json.loads(sidecar.read_text(encoding="utf-8"))

        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = translated

        probe = translated["probe"]
        target = translated["target"]
        start_event = int(task["start_event"])

        with uproot.open(root_path) as f:
            tree = f["gst"]
            try:
                energies_gev = tree["Ev"].array(library="np")
            except Exception as exc:
                raise RuntimeError(f"Cannot read neutrino energy from branch 'Ev': {exc}") from exc
            try:
                weights = tree["wght"].array(library="np")
            except Exception as exc:
                raise RuntimeError(f"Cannot read event weight from branch 'wght': {exc}") from exc
            try:
                flag_qel = tree["qel"].array(library="np")
                flag_res = tree["res"].array(library="np")
                flag_dis = tree["dis"].array(library="np")
                flag_coh = tree["coh"].array(library="np")
                flag_mec = tree["mec"].array(library="np")
            except Exception as exc:
                raise RuntimeError(f"Cannot read interaction flags from 'qel/res/dis/coh/mec': {exc}") from exc
            try:
                # gst stores both four-vectors in GeV: (Ev, pxv, pyv, pzv) is the
                # incoming neutrino, (El, pxl, pyl, pzl) the outgoing primary
                # lepton (the scattered neutrino for NC events).
                nu_p4 = np.column_stack([
                    energies_gev,
                    *(tree[branch].array(library="np") for branch in ("pxv", "pyv", "pzv")),
                ])
                lepton_p4 = np.column_stack([
                    tree[branch].array(library="np") for branch in ("El", "pxl", "pyl", "pzl")
                ])
            except Exception as exc:
                raise RuntimeError(
                    "Cannot read lepton four-vectors from 'pxv/pyv/pzv' and 'El/pxl/pyl/pzl': "
                    f"{exc}"
                ) from exc

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = GenieTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        interactions = [
            interaction_from_flags(qel, res, dis, coh, mec)
            for qel, res, dis, coh, mec in zip(
                flag_qel, flag_res, flag_dis, flag_coh, flag_mec
            )
        ]
        kinematics = derive_kinematics(nu_p4, lepton_p4, interactions)

        events = []
        for i, (ev, w, xw, itype) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions)
        ):
            event = {
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(ev),
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
