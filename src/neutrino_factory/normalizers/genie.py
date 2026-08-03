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
from ..flux import Flux, HistogramFlux
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

    @staticmethod
    def _generated_flux(work_dir: Path, translated: dict) -> Flux:
        """Load the spectrum gevgen actually sampled from.

        gevgen writes the TH1D it hands to its flux driver to ``input-flux.root``
        (``Apps/gEvGen.cxx``, ``TH1FluxDriver``). That file — not the run config —
        is the authoritative generated flux: gevgen zeroes bins outside the ``-e``
        range and, for a TF1 input, Monte-Carlo resamples the function into a
        coarse 300-bin histogram whose noise realization depends on the task seed.
        Dividing events by anything else imprints a sawtooth on ``xsec_weight``.

        Its bin contents are per-bin integrals, hence ``contents_are_counts``.

        Missing file is a hard error: silently falling back to the configured flux
        would restore exactly the bug this guards against, and a wrong
        normalization is worse than no output (see CLAUDE.md, development posture).
        """
        path = work_dir / "input-flux.root"
        if not path.is_file():
            raise RuntimeError(
                f"GENIE's generated flux histogram was not found at {path}. "
                "gevgen writes it into its working directory on every run; without "
                "it the flux the events were drawn from is unknown and xsec_weight "
                "cannot be computed. Re-run the generation step."
            )
        particle = str(translated.get("probe") or "numu")
        return HistogramFlux.from_root_file(
            path, "spectrum", particle, contents_are_counts=True
        )

    def _normalize_gst_root(self, root_path: Path, out_path, task: dict, mode: str) -> str:
        try:
            import uproot
            import awkward as ak
        except ImportError as exc:
            raise RuntimeError(
                "uproot and awkward are required to read GENIE ROOT output. "
                "Install them with: pip install uproot awkward"
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
                # gst carries the current as its own boolean branch, independent
                # of the qel/res/dis/... class flags, which span both currents.
                flag_cc = tree["cc"].array(library="np")
            except Exception as exc:
                raise RuntimeError(f"Cannot read the current flag from branch 'cc': {exc}") from exc
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
            try:
                # The post-FSI final state: pdgf/Ef/pxf/pyf/pzf are gst's jagged
                # per-event arrays of the particles that leave the nucleus (the
                # 'f' family, as opposed to the pre-FSI 'i' one). They include
                # the outgoing lepton and the residual nucleus; both are filtered
                # out by summarize_final_state.
                final_state_arrays = flatten_particle_arrays(
                    ak,
                    tree["pdgf"].array(library="ak"),
                    *(
                        tree[branch].array(library="ak")
                        for branch in ("Ef", "pxf", "pyf", "pzf")
                    ),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read the final-state particle list from 'pdgf/Ef/pxf/pyf/pzf': {exc}"
                ) from exc
            try:
                # GENIE's own channel code, carried verbatim so the exact split
                # can be recovered. gntpc writes its scattering type re-encoded
                # into NEUT's mode scheme; GENIE has no single native integer of
                # its own in gst.
                native_codes = tree["neut_code"].array(library="np")
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot read GENIE's native interaction code from 'neut_code': {exc}"
                ) from exc

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = self._generated_flux(root_path.parent, translated)
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
        final_state = summarize_final_state(*final_state_arrays)

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = GenieTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

        events = []
        for i, (ev, w, xw, itype, is_cc) in enumerate(
            zip(energies_gev, weights, xsec_weights, interactions, flag_cc)
        ):
            event = {
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(ev),
                "weight": float(w),
                "xsec_weight": float(xw),
                "is_cc": bool(is_cc),
                "interaction": itype,
                "probe": probe,
                "target": target,
                "generator": self.name,
                NATIVE_CODE_FIELD: int(native_codes[i]),
            }
            event.update({field: float(kinematics[field][i]) for field in KINEMATIC_FIELDS})
            event.update(event_fields(final_state, i))
            events.append(event)

        return write_common_hdf5(out_path, metadata, events)
