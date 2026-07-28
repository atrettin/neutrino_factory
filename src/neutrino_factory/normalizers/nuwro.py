from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.nuwro import NuWroTranslator
from .base import OutputNormalizer, interaction_from_flags


# NuWro stores momenta in MeV; the common format is GeV throughout.
MEV_PER_GEV = 1000.0


def _leading_component(values, ak) -> np.ndarray:
    """Take element 0 of each event's particle vector, as a dense float array.

    NuWro's particle branches are jagged. ``e/in`` always holds the beam neutrino
    at index 0, but ``e/out`` can be empty for an event, so short entries are
    padded with zeros and flagged separately by ``_has_leading``.
    """
    padded = ak.fill_none(ak.pad_none(values, 1, axis=1), 0.0)
    return np.asarray(ak.to_numpy(padded[:, 0]), dtype=np.float64)


def _has_leading(values, ak) -> np.ndarray:
    return np.asarray(ak.to_numpy(ak.num(values, axis=1)), dtype=np.int64) > 0


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
                # e/in holds the beam neutrino at index 0 (the struck nucleon
                # follows); e/out holds the primary outgoing lepton at index 0.
                # Components are (t, x, y, z) = (E, px, py, pz) in MeV.
                nu_components = [
                    _leading_component(tree[f"e/in/in.{c}"].array(library="ak"), ak)
                    for c in ("t", "x", "y", "z")
                ]
                energies_mev = nu_components[0]
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read neutrino four-vector from branch 'e/in/in.*': {exc}"
                ) from exc
            try:
                out_arrays = [
                    tree[f"e/out/out.{c}"].array(library="ak") for c in ("t", "x", "y", "z")
                ]
                lepton_components = [_leading_component(values, ak) for values in out_arrays]
                has_lepton = _has_leading(out_arrays[0], ak)
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read outgoing lepton four-vector from branch 'e/out/out.*': {exc}"
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

        energies_gev = np.asarray(energies_mev, dtype=np.float64) / MEV_PER_GEV
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = NuWroTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        nu_p4 = np.column_stack(nu_components) / MEV_PER_GEV
        lepton_p4 = np.column_stack(lepton_components) / MEV_PER_GEV
        interactions = [
            interaction_from_flags(qel, res, dis, coh, mec)
            for qel, res, dis, coh, mec in zip(
                flag_qel, flag_res, flag_dis, flag_coh, flag_mec
            )
        ]
        kinematics = derive_kinematics(nu_p4, lepton_p4, interactions, valid=has_lepton)

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = NuWroTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

        events = []
        for i, (e_gev, w, xw, itype) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions)
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
