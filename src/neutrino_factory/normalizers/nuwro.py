from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..translators.nuwro import NuWroTranslator
from .base import OutputNormalizer


class NuWroNormalizer(OutputNormalizer):
    name = "nuwro"

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
            import awkward as ak
        except ImportError as exc:
            raise RuntimeError(
                "uproot and awkward are required to read NuWro ROOT output. "
                "Install them with: pip install uproot awkward"
            ) from exc

        sidecar = root_path.parent / "translated_config.json"
        if not sidecar.exists():
            raise RuntimeError(
                f"translated_config.json not found alongside {root_path}. "
                "Re-run with the current NuWroAdapter to generate it."
            )
        translated = json.loads(sidecar.read_text(encoding="utf-8"))

        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = translated

        probe = translated["beam_particle"]
        target = translated["nucleus"]
        start_event = int(task["start_event"])

        with uproot.open(root_path) as f:
            tree = f["treeout"]
            try:
                energies_mev = ak.to_numpy(tree["e/in/in.t"].array(library="ak")[:, 0])
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read neutrino energy from branch 'e/in/in.t': {exc}"
                ) from exc
            try:
                weights = tree["e/weight"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read event weight from branch 'e/weight': {exc}"
                ) from exc
            try:
                flag_qel = tree["e/flag/flag.qel"].array(library="np")
                flag_res = tree["e/flag/flag.res"].array(library="np")
                flag_dis = tree["e/flag/flag.dis"].array(library="np")
                flag_coh = tree["e/flag/flag.coh"].array(library="np")
                flag_mec = tree["e/flag/flag.mec"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read interaction flags from 'e/flag/flag.*': {exc}"
                ) from exc

        energies_gev = np.asarray(energies_mev, dtype=np.float64) / 1000.0
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = NuWroTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        events = []
        for i, (e_gev, w, xw, qel, res, dis, coh, mec) in enumerate(
            zip(energies_gev, weights, xsec_weights, flag_qel, flag_res, flag_dis, flag_coh, flag_mec)
        ):
            if qel:
                itype = "qel"
            elif res:
                itype = "res"
            elif dis:
                itype = "dis"
            elif coh:
                itype = "coh"
            elif mec:
                itype = "mec"
            else:
                itype = "other"
            events.append({
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "interaction": itype,
                "probe": probe,
                "target": target,
                "generator": self.name,
            })

        return write_common_hdf5(out_path, metadata, events)
