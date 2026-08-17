from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..common_output import version_metadata, write_common_hdf5
from ..flux import build_flux
from ..kinematics import KINEMATIC_FIELDS, derive_kinematics
from ..translators.gibuu import GiBUUTranslator
from .base import OutputNormalizer, resonant_primary_from_interaction


# GiBUU writes one perturbative-event file per run: with
# num_runs_SameEnergy = N it produces EventOutput.Pert.00000001.root through
# ...0000000N.root. Reading only the first while dividing the weights by N (as
# compute_xsec_weight does) would report sigma/N and silently drop the other
# runs' events, so every part must be read.
PERT_OUTPUT_GLOB = "EventOutput.Pert.*.root"

# Subdirectory of the task work directory each weak current's pass runs in, in
# the order their events are concatenated. The adapter creates one per pass (see
# GiBUUAdapter.build_run_command); an inclusive run has both, a cc/nc run one.
PASS_DIR_NAMES = ("cc", "nc")


def pert_output_parts(directory: Path) -> list[Path]:
    """Every GiBUU perturbative-event file in ``directory``, in run order."""
    return sorted(Path(directory).glob(PERT_OUTPUT_GLOB))


def pass_output_dirs(work_dir: Path) -> list[tuple[str, Path]]:
    """The ``(current, directory)`` pairs a run actually produced output in."""
    return [
        (name, work_dir / name)
        for name in PASS_DIR_NAMES
        if (work_dir / name).is_dir() and pert_output_parts(work_dir / name)
    ]


# Highest GiBUU final-state code (``max_finalstate_ID`` in
# code/init/neutrino/initNeutrino.f90). The code space is closed and fully
# documented there, so an out-of-range value means our reading of the output is
# wrong, not that GiBUU produced an exotic event -- hence the hard error below.
MAX_GIBUU_EVTYPE = 37

# The evType span of the non-strange baryon resonances (2 = Delta). Everything
# else in the inelastic range -- 32/33 (1pi background), 34 (DIS), 37 (2pi
# background) -- is non-resonant, which is what `resonant_primary` records.
MIN_RESONANCE_EVTYPE = 2
MAX_RESONANCE_EVTYPE = 31

# 2p2h codes, for which the struck system is a nucleon *pair* but the output
# carries only one of the two. GiBUU's own 2p2h cross section is built from
# `eN%boson%mom + eN%nucleon%mom + eN%nucleon2%mom` (lepton2p2h.f90), yet
# `doStoreNeutrinoInfo` (initNeutrino.f90) hands neutrinoProdInfo only
# `eN%nucleon`, and `nucleon2` never reaches the RootTuple -- it survives solely
# in the nuclear-residue bookkeeping. So w_true_gev cannot be computed here at
# all: filling it from the single stored nucleon would silently produce a
# one-nucleon invariant mass where every other generator supplies the pair,
# ~1 GeV lower and indistinguishable from a real value downstream. Those events
# get the placeholder instead.
TWO_NUCLEON_EVTYPES = (35, 36)


def _interaction_from_evtype(ev_type: int) -> str:
    """Map GiBUU's ``evType`` event-class code to the common interaction label.

    ``evType`` in the RootTuple output is GiBUU's ``prod_id``
    (code/inputOutput/EventOutput.f90), whose authoritative table is
    code/init/neutrino/initNeutrino.f90 (``max_finalstate_ID = 37``):

    * 1: nucleon (QE)
    * 2-31: non-strange baryon resonance (2 = Delta)
    * 32: pi neutron-background (e.g. nu + n -> mu + pi+ + n)
    * 33: pi proton-background  (e.g. nu + n -> mu + pi0 + p)
    * 34: DIS
    * 35: 2p2h QE
    * 36: 2p2h Delta
    * 37: two pion background

    The 1-pion and 2-pion backgrounds (32, 33, 37) are GiBUU's *non-resonant*
    shallow-inelastic contribution, generated for 1.2 < W < ``REScutW`` from a
    MAID-like amplitude with the resonances subtracted (or the Bosted-Christy
    background fit) and switched off exactly where the PYTHIA/DIS piece switches
    on. They are labelled ``dis`` to match GENIE, which has no separate shallow
    category: GENIE's non-resonant background comes from its DIS generator with
    the KNO multiplicity tune applied below ``Wcut``, and so carries the gst
    ``dis`` flag. GiBUU's own NuHepMC exporter instead calls them SIS -- see
    docs/generators/gibuu.md for why we follow GENIE here.
    """
    if ev_type == 1:
        return "qel"
    if 2 <= ev_type <= 31:
        return "res"
    if ev_type in (32, 33, 34, 37):
        return "dis"
    if ev_type in (35, 36):
        return "mec"
    raise ValueError(
        f"Unknown GiBUU evType {ev_type}: outside the documented code range "
        f"1..{MAX_GIBUU_EVTYPE} (max_finalstate_ID in initNeutrino.f90). "
        "Refusing to guess an interaction category for it."
    )


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
        if path.is_dir():
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
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Read and concatenate the ``RootTuple`` branches across run files.

        Concatenation is the right combination here, unlike merging chunks: the
        runs are parts of *one* estimate whose weights are all divided by the
        same ``num_runs`` in ``compute_xsec_weight``, so summing them recovers
        sigma rather than a multiple of it.
        """
        import uproot

        columns: dict[str, list[np.ndarray]] = {
            "energies": [], "weights": [], "ev_types": [],
            "nu_p4": [], "lepton_p4": [], "nucleon_p4": [],
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
                # The struck nucleon, for w_true_gev. Written from the same
                # neutrinoProdInfo block as weight/evType/lepIn_*, so it is filled
                # for every event that is in the file at all — no validity mask is
                # needed. GiBUU's default storeNucleon = 2 stores the *bound*
                # nucleon, so its invariant mass sits below M_N.
                columns["nucleon_p4"].append(np.column_stack([
                    branch(b) for b in ("nuc_E", "nuc_Px", "nuc_Py", "nuc_Pz")
                ]))

        return (
            np.concatenate(columns["energies"]),
            np.concatenate(columns["weights"]),
            np.concatenate(columns["ev_types"]),
            np.concatenate(columns["nu_p4"]),
            np.concatenate(columns["lepton_p4"]),
            np.concatenate(columns["nucleon_p4"]),
        )

    def _normalize_root(self, work_dir: Path, out_path, task: dict, mode: str) -> str:
        try:
            import uproot
        except ImportError as exc:
            raise RuntimeError(
                "uproot is required to read GiBUU ROOT output. "
                "Install it with: pip install uproot"
            ) from exc

        sidecar = work_dir / "translated_config.json"
        if not sidecar.exists():
            raise RuntimeError(
                f"translated_config.json not found in {work_dir}. "
                "Re-run with the current GiBUUAdapter to generate it."
            )
        translated = json.loads(sidecar.read_text(encoding="utf-8"))

        metadata = version_metadata(self.name, task, mode)
        metadata["translated_config"] = translated

        probe = translated["beam_particle"]
        target = translated["nucleus"]
        start_event = int(task["start_event"])

        # One pass per weak current, each in its own subdirectory. Their events
        # are concatenated: GiBUU's weights are absolute cross sections, so the
        # union of a CC and an NC pass is the inclusive sample (see
        # translators.gibuu.CURRENT_PASSES).
        expected_currents = [
            str(gibuu_pass["current"]) for gibuu_pass in translated.get("gibuu_passes", [])
        ]
        pass_dirs = pass_output_dirs(work_dir)
        found_currents = [name for name, _ in pass_dirs]
        if found_currents != expected_currents:
            raise RuntimeError(
                f"Expected GiBUU output for current(s) {expected_currents or 'none'} "
                f"under {work_dir}, found {found_currents or 'none'}. A missing "
                "pass would drop that current's events while the run still claims "
                "to cover it."
            )

        # A missing run's events would be dropped while compute_xsec_weight still
        # divided by the full num_runs, understating sigma by exactly that ratio.
        expected_parts = max(1, int(translated.get("num_runs", 1)))
        parts_by_current: list[tuple[str, list[Path]]] = []
        for pass_current, pass_dir in pass_dirs:
            parts = pert_output_parts(pass_dir)
            if len(parts) != expected_parts:
                raise RuntimeError(
                    f"Expected {expected_parts} GiBUU perturbative output file(s) "
                    f"(num_runs={expected_parts}) in {pass_dir}, found "
                    f"{len(parts)}: {', '.join(p.name for p in parts) or 'none'}. "
                    "The cross section is normalized per run, so a missing run "
                    "would silently understate it."
                )
            parts_by_current.append((pass_current, parts))

        # Read pass by pass, so each event's current is known from the pass it
        # came from — GiBUU's RootTuple has no per-event current branch, and does
        # not need one: a pass generates a single process_ID by construction.
        per_pass = [
            (pass_current, self._read_parts(parts))
            for pass_current, parts in parts_by_current
        ]
        energies_gev, weights, ev_types, nu_p4, lepton_p4, nucleon_p4 = [
            np.concatenate([columns[index] for _, columns in per_pass])
            for index in range(6)
        ]
        is_cc_flags = np.concatenate(
            [
                np.full(len(columns[0]), pass_current == "cc", dtype=bool)
                for pass_current, columns in per_pass
            ]
        )

        energies_gev = np.asarray(energies_gev, dtype=np.float64)
        weights_arr = np.asarray(weights, dtype=np.float64)
        flux = build_flux(translated["flux_config"])
        xsec_weights = GiBUUTranslator().compute_xsec_weight(
            energies_gev, weights_arr, translated, flux
        )

        interactions = [_interaction_from_evtype(int(ev_type)) for ev_type in ev_types]
        # GiBUU's evType names the mechanism directly: 2-31 are non-strange baryon
        # resonances, while 32/33 (1pi background), 34 (DIS) and 37 (2pi
        # background) are the non-resonant pieces. So, as for GENIE, the mechanism
        # follows from the code already read.
        resonant_primary = [
            resonant_primary_from_interaction(
                itype, MIN_RESONANCE_EVTYPE <= int(ev_type) <= MAX_RESONANCE_EVTYPE
            )
            for itype, ev_type in zip(interactions, ev_types)
        ]
        kinematics = derive_kinematics(
            nu_p4,
            lepton_p4,
            interactions,
            nucleon_p4=nucleon_p4,
            nucleon_valid=~np.isin(ev_types, TWO_NUCLEON_EVTYPES),
        )

        # Declare how much this chunk's estimate is worth, so merging averages
        # the chunks instead of summing them (see ConfigTranslator.xsec_norm_count
        # and merge_hdf5_files). Recorded only on the real path: stub output
        # carries placeholder weights that must not be rescaled.
        metadata["xsec_norm_count"] = GiBUUTranslator().xsec_norm_count(
            translated, len(energies_gev)
        )

        events = []
        for i, (e_gev, w, xw, itype, is_cc) in enumerate(
            zip(energies_gev, weights_arr, xsec_weights, interactions, is_cc_flags)
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
