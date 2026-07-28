from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.gibuu import GiBUUTranslator
from .base import OutputNormalizer


# GiBUU writes one perturbative-event file per run: with
# num_runs_SameEnergy = N it produces EventOutput.Pert.00000001.root through
# ...0000000N.root. Reading only the first while dividing the weights by N (as
# compute_xsec_weight does) would report sigma/N and silently drop the other
# runs' events, so every part must be read.
PERT_OUTPUT_GLOB = "EventOutput.Pert.*.root"


def pert_output_parts(directory: Path) -> list[Path]:
    """Every GiBUU perturbative-event file in ``directory``, in run order."""
    return sorted(Path(directory).glob(PERT_OUTPUT_GLOB))


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

    @staticmethod
    def _read_parts(
        parts: list[Path],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Read and concatenate the ``RootTuple`` branches across run files.

        Concatenation is the right combination here, unlike merging chunks: the
        runs are parts of *one* estimate whose weights are all divided by the
        same ``num_runs`` in ``compute_xsec_weight``, so summing them recovers
        sigma rather than a multiple of it.
        """
        import uproot

        columns: dict[str, list[np.ndarray]] = {
            "energies": [], "weights": [], "ev_types": [], "nu_p4": [], "lepton_p4": []
        }
        for part in parts:
            with uproot.open(part) as f:
                tree = f["RootTuple"]

                def branch(name: str) -> np.ndarray:
                    try:
                        return tree[name].array(library="np")
                    except Exception as exc:
                        raise RuntimeError(
                            f"Cannot read branch '{name}' from {part.name}: {exc}"
                        ) from exc

                # GiBUU writes lepIn_E in GeV; it is the incoming neutrino energy.
                energies = branch("lepIn_E")
                columns["energies"].append(energies)
                columns["weights"].append(branch("weight"))
                columns["ev_types"].append(branch("evType"))
                # Both four-vectors are in GeV: lepIn_* is the incoming neutrino,
                # lepOut_* the outgoing lepton (the scattered neutrino for NC).
                columns["nu_p4"].append(np.column_stack([
                    energies, *(branch(b) for b in ("lepIn_Px", "lepIn_Py", "lepIn_Pz"))
                ]))
                columns["lepton_p4"].append(np.column_stack([
                    branch(b) for b in ("lepOut_E", "lepOut_Px", "lepOut_Py", "lepOut_Pz")
                ]))

        return (
            np.concatenate(columns["energies"]),
            np.concatenate(columns["weights"]),
            np.concatenate(columns["ev_types"]),
            np.concatenate(columns["nu_p4"]),
            np.concatenate(columns["lepton_p4"]),
        )

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

        parts = pert_output_parts(root_path.parent) or [root_path]
        # A missing run's events would be dropped while compute_xsec_weight still
        # divided by the full num_runs, understating sigma by exactly that ratio.
        expected_parts = max(1, int(translated.get("num_runs", 1)))
        if len(parts) != expected_parts:
            raise RuntimeError(
                f"Expected {expected_parts} GiBUU perturbative output file(s) "
                f"(num_runs={expected_parts}) in {root_path.parent}, found "
                f"{len(parts)}: {', '.join(p.name for p in parts) or 'none'}. "
                "The cross section is normalized per run, so a missing run would "
                "silently understate it."
            )

        energies_gev, weights, ev_types, nu_p4, lepton_p4 = self._read_parts(parts)

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = GiBUUTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        interactions = [_interaction_from_evtype(int(ev_type)) for ev_type in ev_types]
        kinematics = derive_kinematics(nu_p4, lepton_p4, interactions)

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = GiBUUTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

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
