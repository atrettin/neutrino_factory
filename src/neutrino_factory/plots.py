"""Plot common-output HDF5 files produced by the generators.

Given one file in the common output format (see ``common_output.py``), this
module renders four diagnostic plots as separate PNG images:

1. a stacked horizontal bar of the event counts broken down by interaction
   type, with the expected event count indicated;
2. a histogram of the raw (unweighted) simulated event energies (log-scaled
   energy, linear count);
3. a histogram of the same energies weighted by the per-event ``weight``
   column (log-scaled energy, weighted count);
4. the cross section vs. energy broken down by interaction type, using the
   physically normalized per-event ``xsec_weight`` column (1e-38 cm^2 per
   target nucleon). Inclusive datasets (events of both weak currents) get one
   panel per current, sharing the y-axis.

Every figure is titled with the dataset it shows (neutrino, target, generator,
code version and config version); what is plotted is stated by the axis labels
and not repeated in the title.

Given a whole run configuration, :func:`make_config_plots` renders those four
plots for every merged output the run is expected to produce, plus a six-panel
figure comparing the datasets channel by channel (one figure per weak current).

The plot data helpers never render; ``make_plots`` and ``make_config_plots``
write the files. Run ad-hoc on a single file as::

    python -m neutrino_factory.plots output/merged/run_genie_ver.h5
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import h5py
import numpy as np

from . import layout
from .jobs import safe_token
from .validate_output import expected_outputs

# Canonical interaction-type ordering; any other observed label (e.g. stub
# mode's ``inclusive``) is appended so nothing is dropped.
INTERACTION_ORDER = ("qel", "res", "dis", "coh", "mec", "other")

# Panels of the channel-comparison figure: interaction label -> display name.
# Any observed label without a panel of its own is shown in "Other", so an
# unexpected or generator-specific label is never silently dropped.
CHANNEL_PANELS = (
    ("qel", "QE"),
    ("res", "RES"),
    ("mec", "MEC"),
    ("dis", "DIS"),
    ("coh", "COH"),
    ("other", "Other"),
)

# Display names of the weak currents, used in legend titles and suptitles.
CURRENT_LABELS = {"cc": "CC", "nc": "NC"}

# Default number of log-spaced energy bins for the energy/xsec histograms.
DEFAULT_BINS = 150

XSEC_YLABEL = (
    "$\\sigma(E) / E$ ($10^{-38} \\mathrm{cm}^2 / \\mathrm{nucleon} / \\mathrm{GeV}$)"
)


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


@dataclass(frozen=True)
class PlotData:
    """Everything the plots need from one common-output file."""

    metadata: dict[str, Any]
    energies: np.ndarray
    weights: np.ndarray
    xsec_weights: np.ndarray
    interactions: np.ndarray
    is_cc: np.ndarray
    probe: str
    target: str
    actual_count: int


def _decode_column(values: np.ndarray) -> np.ndarray:
    """Decode an HDF5 string column into a numpy array of ``str``."""
    return np.asarray(
        [v.decode() if isinstance(v, bytes) else str(v) for v in values], dtype=object
    )


def _unique_label(values: np.ndarray, fallback: str = "unknown") -> str:
    """The single distinct value of a string column.

    A common-output file describes one (probe, target) run, so this is normally
    a single value. Should a file ever mix them (a merge of unlike runs), all
    observed values are joined rather than one being silently picked.
    """
    labels = sorted(set(values.tolist()))
    if not labels:
        return fallback
    return ", ".join(labels)


def read_plot_data(path: str | Path) -> PlotData:
    """Read only what the plots need from a common-output file.

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
        xsec_weights = np.asarray(events["xsec_weight"][()], dtype=np.float64)
        is_cc = np.asarray(events["is_cc"][()], dtype=bool)
        interactions = _decode_column(events["interaction"][()])
        probe = _unique_label(_decode_column(events["probe"][()]))
        target = _unique_label(_decode_column(events["target"][()]))

        actual_count = len(energies)
        if "run" in handle and "event_count" in handle["run"].attrs:
            actual_count = int(handle["run"].attrs["event_count"])

    return PlotData(
        metadata=metadata,
        energies=energies,
        weights=weights,
        xsec_weights=xsec_weights,
        interactions=interactions,
        is_cc=is_cc,
        probe=probe,
        target=target,
        actual_count=actual_count,
    )


def dataset_title(data: PlotData) -> str:
    """Full dataset identity for a figure title.

    Names the neutrino, the target and the generator with both version axes.
    What is plotted is left to the axis labels.
    """
    generator = str(data.metadata.get("generator", "unknown"))
    code_version = str(data.metadata.get("code_version", "unknown"))
    config_version = str(data.metadata.get("config_version", "unknown"))
    return f"{data.probe} on {data.target} — {generator} {code_version} / {config_version}"


def dataset_legend_label(data: PlotData) -> str:
    """Compact dataset identity for a legend entry: generator + version."""
    generator = str(data.metadata.get("generator", "unknown"))
    version_id = data.metadata.get("generator_version_id")
    if version_id is None:
        version_id = (
            f"{data.metadata.get('code_version', 'unknown')}"
            f"+{data.metadata.get('config_version', 'unknown')}"
        )
    return f"{generator} {version_id}"


def interaction_counts(interactions: Sequence[str] | np.ndarray) -> dict[str, int]:
    """Count events per interaction type in canonical order.

    Known types (:data:`INTERACTION_ORDER`) come first in their fixed order;
    any other observed label is appended in sorted order so it is never lost.
    """
    tally = Counter(np.asarray(interactions).tolist())
    ordered: dict[str, int] = {}
    for itype in INTERACTION_ORDER:
        if tally.get(itype):
            ordered[itype] = tally[itype]
    for itype in sorted(tally):
        if itype not in ordered:
            ordered[itype] = tally[itype]
    return ordered


def currents_present(is_cc: np.ndarray) -> tuple[str, ...]:
    """Which weak currents the events of a dataset actually contain.

    Derived from the data rather than from ``physics.current`` so that a single
    file is enough to know what to plot, and so no panel is drawn for a current
    the file holds no events for.
    """
    currents: list[str] = []
    if bool(np.any(is_cc)):
        currents.append("cc")
    if bool(np.any(~is_cc)):
        currents.append("nc")
    return tuple(currents)


def log_bin_edges(energies: np.ndarray, bins: int = DEFAULT_BINS) -> np.ndarray:
    """``bins`` log-spaced bin edges spanning the positive energies.

    Falls back to a unit linear range when there is nothing positive to span
    (an empty selection), so the histogram machinery still gets valid edges.
    Callers that plot several subsets of the same data (per-current panels,
    per-dataset comparison lines) must share one edge array, or the resulting
    histograms are not comparable.
    """
    positive = energies[energies > 0]
    if not positive.size:
        return np.linspace(0.0, 1.0, bins + 1)

    low, high = float(positive.min()), float(positive.max())
    if low == high:
        # A single distinct energy would collapse every edge onto one value and
        # give zero-width bins; pad it into a narrow but valid range.
        low, high = low / 1.05, high * 1.05
    return np.logspace(np.log10(low), np.log10(high), bins + 1)


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
    ax.legend(loc="upper right", fontsize="small")


def plot_energy(
    energies: np.ndarray,
    ax,
    weights: np.ndarray | None = None,
    bins: int = DEFAULT_BINS,
) -> None:
    """Histogram of simulated event energies (log-scaled energy), step-style.

    Each bin's (weighted or raw) event count is divided by the bin width, so
    the y-axis is a count per unit energy (GeV^-1) that is comparable across
    the unequal log-spaced bins. When ``weights`` is given each event
    contributes its weight; otherwise every event counts once. The y-axis
    label reflects which of the two is shown. ``bins`` sets the number of
    log-spaced energy bins.

    Errors are the standard weighted-count error, ``sqrt(sum(weight^2))`` per
    bin (for unweighted counts this reduces to the usual ``sqrt(count)``,
    since every weight is 1), scaled by the same bin-width division as the
    counts, and shown as a shaded band around the step histogram.
    """
    bin_edges = log_bin_edges(energies, bins)

    squared_weights = None if weights is None else weights**2
    counts, edges = np.histogram(energies, bins=bin_edges, weights=weights)
    sum_sq, _ = np.histogram(energies, bins=bin_edges, weights=squared_weights)
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
    else:
        ax.set_ylabel("weighted number of events / GeV")


def _xsec_density(
    energies: np.ndarray, xsec_weights: np.ndarray, bin_edges: np.ndarray
) -> np.ndarray:
    """Cross section per bin, divided by bin width and by energy.

    By convention we plot the cross section divided by energy, so each bin's
    summed ``xsec_weight`` is divided by the bin width and by the bin's
    geometric centre.
    """
    counts, edges = np.histogram(energies, bins=bin_edges, weights=xsec_weights)
    density = counts / np.diff(edges)
    if np.all(edges > 0):
        return density / np.sqrt(edges[:-1] * edges[1:])
    # Degenerate edges (no positive energies anywhere) start at zero, so no
    # sigma(E)/E exists — and there are no events in them either.
    return np.zeros_like(density)


def plot_xsec_by_interaction(
    energies: np.ndarray,
    xsec_weights: np.ndarray,
    interactions: Sequence[str] | np.ndarray,
    ax,
    bin_edges: np.ndarray | None = None,
    bins: int = DEFAULT_BINS,
    legend_title: str | None = None,
) -> None:
    """Cross section vs. energy, broken down by interaction type.

    One step-histogram line per interaction type (canonical order first),
    built the same way as :func:`plot_energy` (log-spaced bins, sum per bin
    divided by bin width) but weighted by ``xsec_weight`` instead of
    ``weight``, and divided by energy. Units: 1e-38 cm^2 per target nucleon
    per GeV. ``bin_edges`` shares one binning across panels; when omitted,
    ``bins`` log-spaced bins are derived from ``energies``. ``legend_title``
    states what the panel is restricted to (e.g. the weak current).
    """
    interactions_arr = np.asarray(interactions)
    edges = log_bin_edges(energies, bins) if bin_edges is None else bin_edges

    total_density = np.zeros(len(edges) - 1, dtype=np.float64)
    for itype in interaction_counts(interactions_arr):
        mask = interactions_arr == itype
        density = _xsec_density(energies[mask], xsec_weights[mask], edges)
        total_density += density
        ax.stairs(density, edges, fill=False, label=itype)

    ax.stairs(
        total_density, edges, fill=False, color="black", linestyle="--", label="total"
    )
    ax.set_xscale("log")
    ax.set_xlabel("energy (GeV)")
    ax.set_ylabel(XSEC_YLABEL)
    ax.legend(loc="upper right", fontsize="small", title=legend_title)


def figure_xsec(data: PlotData, plt, bins: int = DEFAULT_BINS):
    """Cross-section figure: one panel per weak current present in the data.

    An inclusive dataset gets a CC and an NC panel sharing the y-axis, so the
    two currents are directly comparable; a single-current dataset stays one
    panel. Which current a panel shows is stated by its legend title. All
    panels share one binning derived from the whole file.
    """
    currents = currents_present(data.is_cc) or ("cc",)
    fig, raw_axes = plt.subplots(
        1, len(currents), figsize=(8 * len(currents), 5), sharey=True, sharex=True
    )
    axes = np.atleast_1d(raw_axes)
    bin_edges = log_bin_edges(data.energies, bins)

    for ax, current in zip(axes, currents):
        mask = data.is_cc if current == "cc" else ~data.is_cc
        plot_xsec_by_interaction(
            data.energies[mask],
            data.xsec_weights[mask],
            data.interactions[mask],
            ax,
            bin_edges=bin_edges,
            legend_title=CURRENT_LABELS[current],
        )
    # The panels share the y-axis, so only the leftmost needs its label.
    for ax in axes[1:]:
        ax.set_ylabel("")

    fig.suptitle(dataset_title(data))
    return fig


def _channel_mask(interactions: np.ndarray, channel: str) -> np.ndarray:
    """Events belonging to one panel of the channel-comparison figure.

    The ``other`` panel collects every label that has no panel of its own, so
    an unexpected or generator-specific interaction label is never dropped.
    """
    if channel != "other":
        return interactions == channel
    named = sorted({label for label, _ in CHANNEL_PANELS} - {"other"})
    return ~np.isin(interactions, named)


def figure_channel_comparison(
    datasets: Sequence[PlotData],
    current: str,
    plt,
    bins: int = DEFAULT_BINS,
    run_name: str | None = None,
    group_label: str | None = None,
    labels: Sequence[str] | None = None,
):
    """Six-panel per-channel cross-section comparison across datasets.

    One panel per entry of :data:`CHANNEL_PANELS`, each showing one line per
    dataset, restricted to ``current``. Panels share the log energy axis and
    one binning computed across every dataset, but keep independent y-axes so
    a small channel (COH) stays readable next to a large one (DIS). A single
    figure-level legend maps line colours to datasets; each dataset keeps its
    colour in every panel.

    ``group_label`` names the initial state the comparison is restricted to
    (``"numu on C12"``) — only datasets sharing one belong in one figure, since
    cross sections on different nuclei are not comparable quantities.
    ``labels`` overrides the legend entries, for the case where two datasets
    share a generator and version and differ in something the default label
    (generator + version) does not show.
    """
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True)
    flat_axes = list(np.asarray(axes).ravel())

    want_cc = current == "cc"
    selections = [
        (data, data.is_cc if want_cc else ~data.is_cc) for data in datasets
    ]
    all_energies = (
        np.concatenate([data.energies[mask] for data, mask in selections])
        if selections
        else np.zeros(0, dtype=np.float64)
    )
    bin_edges = log_bin_edges(all_energies, bins)

    handles: list[Any] = []
    legend_labels: list[str] = []
    for index, (data, current_mask) in enumerate(selections):
        energies = data.energies[current_mask]
        xsec_weights = data.xsec_weights[current_mask]
        interactions = data.interactions[current_mask]
        color = f"C{index % 10}"
        artist = None
        for ax, (channel, _label) in zip(flat_axes, CHANNEL_PANELS):
            mask = _channel_mask(interactions, channel)
            density = _xsec_density(energies[mask], xsec_weights[mask], bin_edges)
            artist = ax.stairs(density, bin_edges, fill=False, color=color)
        handles.append(artist)
        legend_labels.append(
            labels[index] if labels is not None else dataset_legend_label(data)
        )

    columns = len(flat_axes) // 2
    for position, (ax, (_channel, label)) in enumerate(zip(flat_axes, CHANNEL_PANELS)):
        ax.set_title(label)
        ax.set_xscale("log")
        ax.set_xlabel("energy (GeV)")
        # The y-axes are independent but all carry the same quantity, so the
        # long label goes on the leftmost panel of each row only.
        if position % columns == 0:
            ax.set_ylabel(XSEC_YLABEL, fontsize="small")

    title = CURRENT_LABELS.get(current, current.upper())
    if group_label:
        title = f"{group_label} — {title}"
    if run_name:
        title = f"{run_name} — {title}"
    fig.suptitle(title)
    fig.legend(
        handles,
        legend_labels,
        loc="lower center",
        ncol=min(len(legend_labels), 3),
        fontsize="small",
    )
    # Leave room at the bottom for the shared legend.
    fig.tight_layout(rect=(0.0, 0.08, 1.0, 1.0))
    return fig


def _write_figure(fig, out_path: Path, plt, tight: bool = True) -> str:
    """Save and close a figure, returning the path written."""
    if tight:
        fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return str(out_path)


def make_plots(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    prefix: str | None = None,
    bins: int = DEFAULT_BINS,
    data: PlotData | None = None,
) -> list[str]:
    """Render the four plots for ``input_path`` as separate PNG files.

    Files are ``<prefix>_interactions.png``, ``<prefix>_energy.png`` (raw
    counts), ``<prefix>_energy_weighted.png`` (weighted counts) and
    ``<prefix>_xsec_by_type.png``. ``prefix`` defaults to the input's stem;
    ``output_dir`` defaults to the input's directory. ``bins`` sets the number
    of log-spaced energy bins used by the energy and cross-section histograms
    (default 150; use fewer for small event counts to avoid a noisy/sparse
    plot). ``data`` lets a caller that has already read the file pass it in
    rather than reading it a second time.
    """
    plt = _import_pyplot()

    input_path = Path(input_path)
    out_dir = Path(output_dir) if output_dir is not None else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix if prefix is not None else input_path.stem

    plot_data = read_plot_data(input_path) if data is None else data
    counts = interaction_counts(plot_data.interactions)
    expected = int(plot_data.metadata["expected_events"])
    title = dataset_title(plot_data)

    def _axes_figure(plotter: Callable[[Any], None]):
        fig, ax = plt.subplots(figsize=(8, 5))
        plotter(ax)
        ax.set_title(title)
        return fig

    builders: tuple[tuple[str, Callable[[], Any]], ...] = (
        (
            "interactions",
            lambda: _axes_figure(lambda ax: plot_interactions(counts, expected, ax)),
        ),
        (
            "energy",
            lambda: _axes_figure(lambda ax: plot_energy(plot_data.energies, ax, bins=bins)),
        ),
        (
            "energy_weighted",
            lambda: _axes_figure(
                lambda ax: plot_energy(
                    plot_data.energies, ax, weights=plot_data.weights, bins=bins
                )
            ),
        ),
        ("xsec_by_type", lambda: figure_xsec(plot_data, plt, bins=bins)),
    )

    written: list[str] = []
    for name, builder in builders:
        written.append(_write_figure(builder(), out_dir / f"{stem}_{name}.png", plt))

    return written


def make_config_plots(
    config: dict[str, Any],
    output_dir: str | Path | None = None,
    bins: int = DEFAULT_BINS,
) -> list[str]:
    """Plot every merged output a run configuration is expected to produce.

    Each dataset gets its own set of plots (as in :func:`make_plots`). The
    datasets are then grouped by initial state — neutrino flavour and target
    nucleus — and each group is compared channel by channel in a six-panel
    figure per weak current present within it
    (``<run_name>_<particle>_<nucleus>_comparison_<cc|nc>.png``). Cross sections
    on different nuclei are different quantities, so only generators sharing an
    initial state are ever drawn on the same axes. PNGs go to ``output_dir``,
    by default ``<output_root>/plots``.

    The grouping comes from the run configuration, not from the events' own
    ``probe``/``target`` columns: the configuration is the provenance record, so
    the set of figures a run produces is predictable without reading any HDF5,
    and a generator that writes an unexpected target string cannot split a
    comparison in two — the discrepancy shows up in the per-dataset title
    instead.

    Missing files are reported and skipped so a partially completed run can
    still be inspected; if no merged output exists at all this raises, rather
    than silently writing nothing.
    """
    plt = _import_pyplot()

    merged = expected_outputs(config)["merged"]
    out_dir = Path(output_dir) if output_dir is not None else layout.plots_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)

    groups: dict[tuple[str, str], list[tuple[dict[str, Any], PlotData]]] = {}
    written: list[str] = []
    missing: list[str] = []
    for entry in merged:
        path = Path(entry["path"])
        if not path.exists():
            missing.append(str(path))
            print(f"skipping missing merged output: {path}")
            continue
        data = read_plot_data(path)
        key = (str(entry["particle"]), str(entry["nucleus"]))
        groups.setdefault(key, []).append((entry, data))
        written.extend(make_plots(path, out_dir, prefix=path.stem, bins=bins, data=data))

    if not groups:
        raise RuntimeError(
            "None of the merged outputs this configuration expects exist: "
            + ", ".join(missing or ["(the configuration declares no jobs)"])
        )

    run_name = config["run"]["name"]
    for (particle, nucleus), members in groups.items():
        datasets = [data for _entry, data in members]
        # Two jobs can share a generator and version within one group and differ
        # only in the weak current, which the default legend label would not
        # show; the job label always distinguishes them.
        default_labels = [dataset_legend_label(data) for data in datasets]
        labels = (
            None
            if len(set(default_labels)) == len(default_labels)
            else [str(entry["job_label"]) for entry, _data in members]
        )

        currents: list[str] = []
        for data in datasets:
            for current in currents_present(data.is_cc):
                if current not in currents:
                    currents.append(current)

        for current in currents:
            fig = figure_channel_comparison(
                datasets,
                current,
                plt,
                bins=bins,
                run_name=run_name,
                group_label=f"{particle} on {nucleus}",
                labels=labels,
            )
            filename = (
                f"{safe_token(run_name)}_{safe_token(particle)}_{safe_token(nucleus)}"
                f"_comparison_{current}.png"
            )
            written.append(_write_figure(fig, out_dir / filename, plt, tight=False))

    return written


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neutrino_factory.plots",
        description=(
            "Plot a single common-output HDF5 file (interactions, energies, cross "
            "sections). Use 'neutrino-factory plot-output --config' to plot every "
            "output of a whole run at once."
        ),
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
    parser.add_argument(
        "--bins",
        type=int,
        default=DEFAULT_BINS,
        help=(
            "Number of log-spaced energy bins for the energy and cross-section "
            f"histograms (default: {DEFAULT_BINS}; use fewer for small event counts)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    written = make_plots(args.input, args.output_dir, args.prefix, bins=args.bins)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
