from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..final_state import (
    NATIVE_CODE_FIELD,
    event_fields,
    flatten_particle_arrays,
    summarize_final_state,
)
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.nuwro import NuWroTranslator
from .. import universes
from .base import (
    OutputNormalizer,
    interaction_from_flags,
    resonant_primary_from_interaction,
)


# NuWro stores momenta in MeV; the common format is GeV throughout.
MEV_PER_GEV = 1000.0


def _branch(tree, name: str, library: str):
    """Read the one branch matching ``name``, whatever uproot calls its field.

    The field is taken by position: uproot names it after the leaf (``in.t``) for
    NuWro's split ``event`` object, but after the full path for a flat branch
    whose name contains slashes, as the test fixtures write.
    """
    arrays = tree.arrays(filter_name=name, library=library)
    if library == "np":
        (values,) = arrays.values()
    else:
        import awkward as ak

        (values,) = ak.unzip(arrays)
    return values


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


def _has_nucleon(pdgs, ak) -> np.ndarray:
    """Flag events with a nucleon among the struck initial state in ``e/in``.

    ``e/in`` holds the beam neutrino at index 0 followed by the struck system:
    none for coherent events, one nucleon for qel/res/dis, two or three for MEC,
    and for a few events an atomic *electron*. Only events that scattered off a
    nucleon have a struck-system W, so the selection is by PDG, not by index.
    """
    is_nucleon = ak.zeros_like(pdgs, dtype=bool)
    for code in NUCLEON_PDGS:
        is_nucleon = is_nucleon | (pdgs == code)
    return np.asarray(ak.to_numpy(ak.sum(is_nucleon, axis=1)), dtype=np.int64) > 0


def _outgoing_hadrons(components, is_qel, ak) -> tuple[np.ndarray, np.ndarray]:
    """Sum the pre-FSI outgoing hadronic system, ``e/out[1:]``.

    This is what NuWro's own ``event::W()`` sums, and for RES it is the W NuWro
    sampled and cut on at ``res_dis_cut``. The initial nucleon in ``e/in`` does not
    give it: NuWro solves the RES/DIS vertex against a copy of that nucleon with a
    binding energy subtracted from its energy only, and stores the unbound one. So
    ``p_nu + p_N(e/in) - p_l`` exceeds these hadrons by exactly that binding energy
    (three-momentum balances to 1e-3 MeV); see docs/generators/nuwro.md.

    For qel only ``out[1]`` is taken. With the spectral function, a correlated
    event also emits the SRC partner nucleon at ``out[2]`` (sfevent.cc), which is a
    spectator and not part of the vertex.

    Returns the ``(n, 4)`` summed four-vector and the mask of events that had at
    least one outgoing hadron.
    """
    padded = [ak.fill_none(ak.pad_none(values, 2, axis=1), 0.0) for values in components]
    summed = np.column_stack([
        np.asarray(ak.to_numpy(ak.sum(values[:, 1:], axis=1)), dtype=np.float64)
        for values in padded
    ])
    struck = np.column_stack([
        np.asarray(ak.to_numpy(values[:, 1]), dtype=np.float64) for values in padded
    ])
    hadrons = np.where(np.asarray(is_qel, dtype=bool)[:, None], struck, summed)
    count = np.asarray(ak.to_numpy(ak.num(components[0], axis=1)), dtype=np.int64)
    return hadrons, count > 1


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


def _universe_weights(
    work_dir: Path, flags: dict[str, np.ndarray], antineutrino: bool, n_events: int
) -> tuple[dict, np.ndarray]:
    """The resolved universes metadata and the ``(n_events, count)`` weights.

    The product of ``nf_reweight``'s per-universe ratios (for NuWro-reweighted
    parameters) and the channel norm factors (applied here; NuWro's own norm
    engine never runs). Non-finite weights are an error: nf_reweight writes them
    as they are instead of hiding them as 0 the way reweight_to does.
    """
    resolved = universes.read_resolved(work_dir)
    univ = resolved["universes"]
    norms = {
        name: np.asarray(univ["values"][name])
        for name in univ["parameters"]
        if name in universes.NUWRO_NORM_PARAMS
    }
    factors = universes.norm_factors(norms, flags, antineutrino) if norms else None
    weights, _ = universes.load_weights(work_dir, resolved, n_events, factors)
    assert weights is not None
    return univ, weights


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
                # initial state (see _has_nucleon); e/out holds the primary
                # outgoing lepton at index 0, then the pre-FSI hadronic system
                # (see _outgoing_hadrons). Components are (t, x, y, z) =
                # (E, px, py, pz) in MeV.
                in_arrays = [
                    _branch(tree, f"e/in/in.{c}", "ak")
                    for c in ("t", "x", "y", "z")
                ]
                nu_components = [_leading_component(values, ak) for values in in_arrays]
                energies_mev = nu_components[0]
                has_nucleon = _has_nucleon(
                    _branch(tree, "e/in/in.pdg", "ak"), ak
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read initial-state four-vectors from branch 'e/in/in.*': {exc}"
                ) from exc
            try:
                out_arrays = [
                    _branch(tree, f"e/out/out.{c}", "ak")
                    for c in ("t", "x", "y", "z")
                ]
                lepton_components = [_leading_component(values, ak) for values in out_arrays]
                has_lepton = _has_leading(out_arrays[0], ak)
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read outgoing lepton four-vector from branch 'e/out/out.*': {exc}"
                ) from exc
            try:
                weights = _branch(tree, "e/weight", "np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read event weight from branch 'e/weight': {exc}"
                ) from exc
            try:
                # The current is its own flag in the same struct; the class
                # flags below (qel/res/...) span both currents.
                flag_cc = _branch(tree, "e/flag/flag.cc", "np")
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
                res_kind = _branch(tree, "e/par/par.res_kind", "np")
                flag_res_delta = _branch(tree, "e/flag/flag.res_delta", "np")
            except Exception as exc:
                raise RuntimeError(
                    "Cannot read 'e/par/par.res_kind' / 'e/flag/flag.res_delta', "
                    f"needed for the resonant_primary column: {exc}"
                ) from exc
            try:
                flag_qel = _branch(tree, "e/flag/flag.qel", "np")
                flag_res = _branch(tree, "e/flag/flag.res", "np")
                flag_dis = _branch(tree, "e/flag/flag.dis", "np")
                flag_coh = _branch(tree, "e/flag/flag.coh", "np")
                flag_mec = _branch(tree, "e/flag/flag.mec", "np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read interaction flags from 'e/flag/flag.*': {exc}"
                ) from exc
            try:
                # e/post is the post-FSI particle vector -- what actually leaves
                # the nucleus -- as opposed to e/out, the primary vertex the
                # outgoing lepton is taken from above. Components are again
                # (t, x, y, z) = (E, px, py, pz) in MeV.
                fs_pdg, fs_energy, fs_momentum, fs_counts = flatten_particle_arrays(
                    ak,
                    _branch(tree, "e/post/post.pdg", "ak"),
                    *(
                        _branch(tree, f"e/post/post.{c}", "ak")
                        for c in ("t", "x", "y", "z")
                    ),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read the final-state particle list from 'e/post/post.*': {exc}"
                ) from exc
            try:
                # NuWro's own channel code, carried verbatim. It is finer than
                # the common label: dyn distinguishes the CC and NC variant of
                # each dynamics (see translators.nuwro.DYNAMICS_CHANNELS).
                native_codes = _branch(tree, "e/dyn", "np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read NuWro's native interaction code from 'e/dyn': {exc}"
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
        hadrons_p4, has_hadrons = _outgoing_hadrons(
            out_arrays, [itype == "qel" for itype in interactions], ak
        )
        # The binding-corrected struck nucleon NuWro solved the vertex against,
        # which it does not store: it is whatever balances the outgoing hadrons,
        # so derive_kinematics' w_true = |nu + N - l| is |sum of hadrons|.
        bound_nucleon_p4 = hadrons_p4 / MEV_PER_GEV - nu_p4 + lepton_p4
        kinematics = derive_kinematics(
            nu_p4,
            lepton_p4,
            interactions,
            valid=has_lepton,
            nucleon_p4=bound_nucleon_p4,
            nucleon_valid=has_nucleon & has_hadrons,
        )
        final_state = summarize_final_state(
            fs_pdg, fs_energy / MEV_PER_GEV, fs_momentum / MEV_PER_GEV, fs_counts,
            nu_p4[:, 1:], lepton_p4[:, 1:],
        )

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = NuWroTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

        events = []
        for i, (e_gev, w, xw, itype, is_cc, native_code) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions, flag_cc, native_codes)
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
                NATIVE_CODE_FIELD: int(native_code),
            }
            event.update({field: float(kinematics[field][i]) for field in KINEMATIC_FIELDS})
            event.update(event_fields(final_state, i))
            events.append(event)

        universe_weights = None
        if translated.get("universes"):
            flags = {
                "qel": flag_qel, "res": flag_res, "dis": flag_dis, "coh": flag_coh,
                "mec": flag_mec, "cc": flag_cc,
            }
            antineutrino = int(translated["nuwro_params"]["beam_particle"]) < 0
            metadata["universes"], universe_weights = _universe_weights(
                root_path.parent, flags, antineutrino, len(events)
            )

        return write_common_hdf5(out_path, metadata, events, universe_weights=universe_weights)
