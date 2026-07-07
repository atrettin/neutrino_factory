"""Plot a common-output HDF5 file produced by the generators.

Given one file in the common output format (see ``common_output.py``), this
module renders four diagnostic plots as separate PNG images:

1. a stacked horizontal bar of the event counts broken down by interaction
   type, with the expected event count indicated;
2. the simulated flux vs. neutrino energy (log-scaled energy, flux in a.u.);
3. a histogram of the raw (unweighted) simulated event energies (log-scaled
   energy, linear count);
4. a histogram of the same energies weighted by the per-event ``weight``
   column (log-scaled energy, weighted count).

The plot data helpers never render; ``make_plots`` writes the files. Run
ad-hoc as::

    python -m neutrino_factory.plots output/merged/run_genie_ver.h5
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .flux import build_flux

# Canonical interaction-type ordering; any other observed label (e.g. stub
# mode's ``inclusive``) is appended so nothing is dropped.
INTERACTION_ORDER = ("qel", "res", "dis", "coh", "mec", "other")


def _import_pyplot():
    """Import matplotlib's pyplot with a non-interactive backend.

    Guarded like the ``uproot`` import in the normalizers so a missing optional
    native dependency yields a clear, actionable error.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - dependency always present in runtime
        raise RuntimeError(
            "matplotlib is required to plot HDF5 output. "
            "Install it with: pip install matplotlib"
        ) from exc
    return plt


def _read_plot_data(
    path: str | Path,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray, list[str], int]:
    """Read only what the plots need from a common-output file.

    Returns ``(metadata, energies_gev, weights, interactions, actual_count)``.
    Reads the event columns as whole arrays rather than per-event dicts
    (cheaper, and the plots want arrays anyway).
    """
    with h5py.File(path, "r") as handle:
        metadata: dict[str, Any] = {}
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value

        events = handle["events"]
        energies = np.asarray(events["energy_gev"][()], dtype=np.float64)
        weights = np.asarray(events["weight"][()], dtype=np.float64)
        raw_interactions = events["interaction"][()]
        interactions = [
            v.decode() if isinstance(v, bytes) else str(v) for v in raw_interactions
        ]

        actual_count = len(energies)
        if "run" in handle and "event_count" in handle["run"].attrs:
            actual_count = int(handle["run"].attrs["event_count"])

    return metadata, energies, weights, interactions, actual_count


def interaction_counts(interactions: list[str]) -> dict[str, int]:
    """Count events per interaction type in canonical order.

    Known types (:data:`INTERACTION_ORDER`) come first in their fixed order;
    any other observed label is appended in sorted order so it is never lost.
    """
    tally = Counter(interactions)
    ordered: dict[str, int] = {}
    for itype in INTERACTION_ORDER:
        if tally.get(itype):
            ordered[itype] = tally[itype]
    for itype in sorted(tally):
        if itype not in ordered:
            ordered[itype] = tally[itype]
    return ordered


def _flux_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Extract the flux config block from metadata (stored as a JSON string)."""
    flux_meta = metadata["flux"]
    if isinstance(flux_meta, str):
        return json.loads(flux_meta)
    return dict(flux_meta)


def plot_interactions(counts: dict[str, int], expected: int, ax) -> None:
    """Stacked horizontal bar of event counts by interaction type."""
    left = 0.0
    for itype, count in counts.items():
        ax.barh(0, count, left=left, label=f"{itype} ({count})")
        left += count

    ax.axvline(
        expected,
        color="black",
        linestyle="--",
        linewidth=1.0,
        label=f"expected ({expected})",
    )

    ax.set_xlabel("number of events")
    ax.set_yticks([])
    ax.set_title("Events by interaction type")
    ax.legend(loc="upper right", fontsize="small")


def plot_flux(flux_config: dict[str, Any], ax) -> None:
    """Simulated flux vs. energy (log-scaled energy, flux in a.u.)."""
    flux = build_flux(flux_config)
    energies = np.logspace(
        np.log10(flux.emin_gev), np.log10(flux.emax_gev), 200
    )
    density = np.array([flux(float(e)) for e in energies], dtype=np.float64)

    ax.plot(energies, density)
    ax.set_xscale("log")
    ax.set_xlabel("energy (GeV)")
    ax.set_ylabel("flux (a.u.)")
    ax.set_title("Simulated flux")


def plot_energy(
    energies: np.ndarray, ax, weights: np.ndarray | None = None
) -> None:
    """Histogram of simulated event energies (log-scaled energy), step-style.

    Each bin's (weighted or raw) event count is divided by the bin width, so
    the y-axis is a count per unit energy (GeV^-1) that is comparable across
    the unequal log-spaced bins. When ``weights`` is given each event
    contributes its weight; otherwise every event counts once. The y-axis
    label reflects which of the two is shown.

    Errors are the standard weighted-count error, ``sqrt(sum(weight^2))`` per
    bin (for unweighted counts this reduces to the usual ``sqrt(count)``,
    since every weight is 1), scaled by the same bin-width division as the
    counts, and shown as a shaded band around the step histogram.
    """
    positive = energies[energies > 0]
    if positive.size:
        bins = np.logspace(
            np.log10(positive.min()), np.log10(positive.max()), 150
        )
    else:
        bins = np.linspace(0.0, 1.0, 51)

    squared_weights = None if weights is None else weights**2
    counts, edges = np.histogram(energies, bins=bins, weights=weights)
    sum_sq, _ = np.histogram(energies, bins=bins, weights=squared_weights)
    widths = np.diff(edges)
    density = counts / widths
    errors = np.sqrt(sum_sq) / widths

    ax.stairs(density, edges, fill=False, color="C0")
    ax.stairs(
        density + errors,
        edges,
        baseline=density - errors,
        fill=True,
        color="C0",
        alpha=0.3,
    )
    ax.set_xscale("log")
    ax.set_xlabel("energy (GeV)")
    if weights is None:
        ax.set_ylabel("raw number of events / GeV")
        ax.set_title("Simulated event energies (raw)")
    else:
        ax.set_ylabel("weighted number of events / GeV")
        ax.set_title("Simulated event energies (weighted)")


def make_plots(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    prefix: str | None = None,
) -> list[str]:
    """Render the four plots for ``input_path`` as separate PNG files.

    Files are ``<prefix>_interactions.png``, ``<prefix>_flux.png``,
    ``<prefix>_energy.png`` (raw counts) and ``<prefix>_energy_weighted.png``
    (weighted counts). ``prefix`` defaults to the input's stem; ``output_dir``
    defaults to the input's directory.
    """
    plt = _import_pyplot()

    input_path = Path(input_path)
    out_dir = Path(output_dir) if output_dir is not None else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix if prefix is not None else input_path.stem

    metadata, energies, weights, interactions, _actual = _read_plot_data(input_path)
    counts = interaction_counts(interactions)
    expected = int(metadata["expected_events"])
    flux_config = _flux_from_metadata(metadata)

    written: list[str] = []
    for name, plotter in (
        ("interactions", lambda ax: plot_interactions(counts, expected, ax)),
        ("flux", lambda ax: plot_flux(flux_config, ax)),
        ("energy", lambda ax: plot_energy(energies, ax)),
        ("energy_weighted", lambda ax: plot_energy(energies, ax, weights=weights)),
    ):
        fig, ax = plt.subplots(figsize=(8, 5))
        plotter(ax)
        fig.tight_layout()
        out_path = str(out_dir / f"{stem}_{name}.png")
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        written.append(out_path)

    return written


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neutrino_factory.plots",
        description="Plot a common-output HDF5 file (interactions, flux, energies).",
    )
    parser.add_argument("input", help="HDF5 file to plot")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write PNGs into (default: alongside the input)",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Filename prefix for the PNGs (default: the input file stem)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    written = make_plots(args.input, args.output_dir, args.prefix)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
