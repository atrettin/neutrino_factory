from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.nuwro import NuWroTranslator
from .base import (
    OutputNormalizer,
    interaction_from_flags,
    resonant_primary_from_interaction,
)


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


# The initial-state particles that make up the struck hadronic system.
NUCLEON_PDGS = (2112, 2212)


def _summed_nucleons(components, pdgs, ak) -> tuple[np.ndarray, np.ndarray]:
    """Sum the initial-state nucleons in ``e/in``, and flag events that have one.

    ``e/in`` holds the beam neutrino at index 0 followed by the struck hadronic
    system, whose size is *not* fixed: coherent events have none, qel/res/dis one,
    and NuWro's MEC two or three (measured on a 100k-event Ar40 run). A few events
    also carry an atomic *electron* there, which is not part of a nucleonic W.
    So the selection is by PDG rather than by index, and the whole system is
    summed -- for 2p2h the correlated pair is the struck system, matching the
    two-nucleon cluster GENIE hands over for the same events.

    Returns the ``(n, 4)`` summed four-vector and the mask of events that had at
    least one nucleon.
    """
    is_nucleon = ak.zeros_like(pdgs, dtype=bool)
    for code in NUCLEON_PDGS:
        is_nucleon = is_nucleon | (pdgs == code)
    summed = [
        np.asarray(ak.to_numpy(ak.sum(values[is_nucleon], axis=1)), dtype=np.float64)
        for values in components
    ]
    count = np.asarray(ak.to_numpy(ak.sum(is_nucleon, axis=1)), dtype=np.int64)
    return np.column_stack(summed), count > 0


# NuWro's RES model. Only the hybrid model (2, the default) sets flag.res_delta
# on every resonant final state; see the read site for what goes wrong otherwise.
HYBRID_RES_KIND = 2


def _resonant_primary(interactions, res_delta, res_kind) -> list[int]:
    """The ``resonant_primary`` column from NuWro's ``flag.res_delta``.

    ``dyn_dis`` events are non-resonant by construction (they are the PYTHIA/DIS
    channel, and flag.res_delta is false for all of them); within ``dyn_res`` the
    flag separates the resonant term from the blended-in background.
    """
    kinds = set(np.asarray(res_kind).reshape(-1).tolist())
    if kinds != {HYBRID_RES_KIND}:
        raise RuntimeError(
            f"NuWro run used res_kind {sorted(kinds)}, not {HYBRID_RES_KIND} "
            "(the hybrid model). flag.res_delta only marks every resonant final "
            "state under the hybrid model; under resevent2.cc it is left false "
            "below the PYTHIA threshold, which would label the whole Delta peak "
            "non-resonant. Refusing to fill resonant_primary from it."
        )
    delta = np.asarray(res_delta, dtype=bool)
    return [
        resonant_primary_from_interaction(itype, bool(is_delta))
        for itype, is_delta in zip(interactions, delta)
    ]


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
                # e/in holds the beam neutrino at index 0, followed by the struck
                # hadronic system (see _summed_nucleons); e/out holds the primary
                # outgoing lepton at index 0. Components are (t, x, y, z) =
                # (E, px, py, pz) in MeV.
                in_arrays = [
                    tree.arrays(filter_name=f"e/in/in.{c}", library="ak")[f"in.{c}"]
                    for c in ("t", "x", "y", "z")
                ]
                nu_components = [_leading_component(values, ak) for values in in_arrays]
                energies_mev = nu_components[0]
                nucleon_p4_mev, has_nucleon = _summed_nucleons(
                    in_arrays, tree.arrays(filter_name="e/in/in.pdg", library="ak")["in.pdg"], ak
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read initial-state four-vectors from branch 'e/in/in.*': {exc}"
                ) from exc
            try:
                out_arrays = [
                    tree.arrays(filter_name=f"e/out/out.{c}", library="ak")[f"out.{c}"]
                    for c in ("t", "x", "y", "z")
                ]
                lepton_components = [_leading_component(values, ak) for values in out_arrays]
                has_lepton = _has_leading(out_arrays[0], ak)
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read outgoing lepton four-vector from branch 'e/out/out.*': {exc}"
                ) from exc
            try:
                weights = tree.arrays(filter_name="e/weight", library="np")["weight"]
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read event weight from branch 'e/weight': {exc}"
                ) from exc
            try:
                # The current is its own flag in the same struct; the class
                # flags below (qel/res/...) span both currents.
                flag_cc = tree.arrays(filter_name="e/flag/flag.cc", library="np")["flag.cc"]
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read the current flag from 'e/flag/flag.cc': {exc}"
                ) from exc
            try:
                # NuWro is the one generator whose `res` channel mixes mechanisms:
                # over res_dis_blending_start..end it blends non-resonant
                # background into itself, so flag.res alone does not say how the
                # hadronic system was made. flag.res_delta does.
                #
                # Its meaning is model-dependent, which is why res_kind is checked
                # rather than assumed. In the hybrid model (res_kind = 2, NuWro's
                # default) every resonant final state is generated through
                # gen_final_particles_hybrid, which sets the flag. In resevent2.cc
                # (res_kind != 2) the below-PYTHIA-threshold branch emits the
                # nucleon-pion pair without setting it, so the flag would read
                # false across the whole Delta peak and quietly invert the meaning
                # of this column.
                res_kind = tree.arrays(filter_name="e/par/par.res_kind", library="np")["par.res_kind"]
                flag_res_delta = tree.arrays(filter_name="e/flag/flag.res_delta", library="np")["flag.res_delta"]
            except Exception as exc:
                raise RuntimeError(
                    "Cannot read 'e/par/par.res_kind' / 'e/flag/flag.res_delta', "
                    f"needed for the resonant_primary column: {exc}"
                ) from exc
            try:
                flag_qel = tree.arrays(filter_name="e/flag/flag.qel", library="np")["flag.qel"]
                flag_res = tree.arrays(filter_name="e/flag/flag.res", library="np")["flag.res"]
                flag_dis = tree.arrays(filter_name="e/flag/flag.dis", library="np")["flag.dis"]
                flag_coh = tree.arrays(filter_name="e/flag/flag.coh", library="np")["flag.coh"]
                flag_mec = tree.arrays(filter_name="e/flag/flag.mec", library="np")["flag.mec"]
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
        resonant_primary = _resonant_primary(interactions, flag_res_delta, res_kind)
        kinematics = derive_kinematics(
            nu_p4,
            lepton_p4,
            interactions,
            valid=has_lepton,
            nucleon_p4=nucleon_p4_mev / MEV_PER_GEV,
            nucleon_valid=has_nucleon,
        )

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = NuWroTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

        events = []
        for i, (e_gev, w, xw, itype, is_cc) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions, flag_cc)
        ):
            event = {
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "is_cc": bool(is_cc),
                "resonant_primary": resonant_primary[i],
                "interaction": itype,
                "probe": probe,
                "target": target,
                "generator": self.name,
            }
            event.update({field: float(kinematics[field][i]) for field in KINEMATIC_FIELDS})
            events.append(event)

        return write_common_hdf5(out_path, metadata, events)
