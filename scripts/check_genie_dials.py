"""Per-dial checks for GENIE Reweight dials, on the samples of a check run.

The acceptance test every dial in ``universes.GENIE_SCALE_DIALS`` has to pass
(docs/generators/genie.md, "Per-dial checks"): moved alone to scale 1.2, a dial
must leave the nominal weight exactly 1, give only finite weights, move only the
channel it belongs to, and change that channel's mean weight by the expected
sign and size. Run inside nf-base.sif (it calls nf_genie_reweight) after the
check run has finished:

    neutrino-factory submit --config configs/smoke/genie_dialcheck.yaml --executor local
    python scripts/check_genie_dials.py --config configs/smoke/genie_dialcheck.yaml

For each job (chunk 0) it checks every dial of ``--dials`` that validation
accepts on the job's tune and that can act on its probe and current, all in one
nf_genie_reweight call: a nominal row, then one row per dial at 1.2. It writes
``universes.txt`` and ``universe_weights.root`` into the chunk's work directory,
which a job without universes does not otherwise use.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import h5py
import numpy as np

from neutrino_factory import layout, particles, universes
from neutrino_factory.config import load_config
from neutrino_factory.generators.registry import get_adapter

# The dials added after the first check round (default for --dials).
NEW_DIALS = (
    *universes.GENIE_ZEXP_DIALS,
    "CoulombCCQE",
    *(
        name
        for name in universes.GENIE_NONRESBG_DIALS
        if name not in ("NonRESBGvpCC1pi", "NonRESBGvnCC1pi", "NonRESBGvpCC2pi", "NonRESBGvnCC2pi")
    ),
)
SCALE = 1.2


def expected_mask(name: str, columns: dict[str, np.ndarray]) -> np.ndarray:
    """The events a dial may move: its channel (and current), or every event for FSI."""
    interaction, is_cc, w = columns["interaction"], columns["is_cc"], columns["w_true_gev"]
    field = universes.GENIE_DIAL_MODEL[name]
    if field == "fsi":
        return np.ones(len(interaction), dtype=bool)
    if name in universes.GENIE_NONRESBG_DIALS:
        cc = "CC" in name.removeprefix("NonRESBG")
        return (interaction == "dis") & (w < 2.0) & (is_cc == cc)
    channel = {"qel_cc": "qel", "res_cc": "res", "coh_cc_pion": "coh", "dis_cc": "dis",
               "mec_cc": "mec"}[field]
    return (interaction == channel) & is_cc


def read_columns(path: Path) -> dict[str, np.ndarray]:
    with h5py.File(path, "r") as handle:
        events = handle["events"]
        interaction = np.asarray(events["interaction"][()])
        if interaction.dtype.kind in ("S", "O"):
            interaction = np.array(
                [v.decode() if isinstance(v, bytes) else str(v) for v in interaction]
            )
        return {
            "interaction": interaction,
            "is_cc": np.asarray(events["is_cc"][()], dtype=bool),
            "w_true_gev": np.asarray(events["w_true_gev"][()], dtype=float),
        }


def check_job(config: dict, job: dict, dials: list[str]) -> bool:
    tune, code_version = str(job["config_version"]), str(job["code_version"])
    cmc = "_".join(tune.split("_")[:2])
    models = universes.GENIE_TUNE_MODELS[code_version][cmc]
    antineutrino = particles.is_antineutrino(job["flux"]["particle"])
    current = str(job["physics"]["current"]).lower()
    names = [
        name for name in dials
        if universes._genie_dial_problem(name, models) is None
        and universes.genie_inert_dials([name], antineutrino, current) is None
    ]
    print(f"\n== {job['label']}")
    if not names:
        print("   no dial to check on this sample")
        return True

    task = {"run_name": config["run"]["name"], "job_label": job["label"], "chunk_id": 0}
    work_dir = layout.raw_output_path(config, task).parent
    h5 = layout.chunk_output_path(config, task)
    if not (work_dir / "events.ghep.root").is_file() or not h5.is_file():
        print(f"   MISSING: run the check config first ({work_dir}, {h5})")
        return False

    rows = [[1.0] * len(names)] + [
        [SCALE if i == j else 1.0 for i in range(len(names))] for j in range(len(names))
    ]
    universes.write_spec(work_dir / universes.SPEC_FILE, [f"{n}:scale" for n in names], rows)
    adapter = get_adapter("genie", config)
    start = time.monotonic()
    adapter._run_reweight_binary(work_dir, code_version, tune)
    elapsed = time.monotonic() - start

    import uproot

    with uproot.open(work_dir / universes.WEIGHTS_FILE) as f:
        weights = np.asarray(f["weights"]["weights"].array(library="np"), dtype=np.float64)
    columns = read_columns(h5)
    n_events = len(columns["interaction"])
    if weights.shape != (n_events, len(rows)):
        print(f"   FAIL: weights shape {weights.shape}, expected ({n_events}, {len(rows)})")
        return False
    print(f"   {n_events} events, {len(rows)} columns, {elapsed:.0f} s "
          f"({1e3 * elapsed / (n_events * len(rows)):.2f} ms per event per column, incl. startup)")

    ok = True
    nominal = weights[:, 0]
    if not np.all(nominal == 1.0):
        print(f"   FAIL: nominal weight != 1 for {int((nominal != 1.0).sum())} events")
        ok = False
    print(f"   {'dial':<20} {'finite':>6} {'moved':>7} {'outside':>7} {'mean(expected)':>14}")
    for j, name in enumerate(names, start=1):
        w = weights[:, j]
        finite = bool(np.all(np.isfinite(w)))
        expected = expected_mask(name, columns)
        moved = w != 1.0
        outside = int((moved & ~expected).sum())
        if not expected.any():
            # e.g. a CC dial in an NC-only sample: nothing to check, but nothing may move.
            bad = not finite or outside > 0
            ok &= not bad
            print(f"   {name:<20} {str(finite):>6} {int(moved.sum()):>7} {outside:>7} "
                  f"{'no events':>14}" + ("   <- FAIL" if bad else ""))
            continue
        mean = float(np.mean(w[expected]))
        bad = not finite or outside > 0 or not moved.any()
        ok &= not bad
        print(f"   {name:<20} {str(finite):>6} {int(moved.sum()):>7} {outside:>7} {mean:>14.4f}"
              + ("   <- FAIL" if bad else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True, help="the check run's config")
    parser.add_argument("--dials", nargs="+", default=list(NEW_DIALS),
                        help="dials to check (default: the dials added in the second round)")
    args = parser.parse_args()
    unknown = sorted(set(args.dials) - set(universes.GENIE_SCALE_DIALS))
    if unknown:
        parser.error(f"not scale dials: {', '.join(unknown)}")
    for left, right in universes.GENIE_EXCLUSIVE:
        if set(args.dials) & left and set(args.dials) & right:
            parser.error(f"{sorted(left)} and {sorted(right)} select incompatible engine modes; "
                         "check them in separate calls")
    config = load_config(args.config)
    results = [check_job(config, job, args.dials) for job in config["jobs"]]
    print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
