from __future__ import annotations

import json
from pathlib import Path

from ..common_output import version_metadata, write_common_hdf5
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

        events = []
        for i, (e_gev, w, ev_type) in enumerate(zip(energies_gev, weights, ev_types)):
            events.append({
                "event_id": start_event + i,
                "seed": int(task["seed"]),
                "energy_gev": float(e_gev),
                "weight": float(w),
                "interaction": _interaction_from_evtype(int(ev_type)),
                "probe": probe,
                "target": target,
                "generator": self.name,
            })

        return write_common_hdf5(out_path, metadata, events)
