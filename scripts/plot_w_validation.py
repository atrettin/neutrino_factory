"""Render the W validation figures from a set of merged common-output files.

This is the physics-shape check for the two hadronic-mass columns added to the
common format: it draws what `docs/physics.md` asserts about them, so the claims
can be looked at rather than taken on trust.

    python scripts/plot_w_validation.py <merged.h5> [...] --out-dir <dir>

Four figures, all cross-section weighted:

1. ``w_definitions`` — one panel per generator overlaying ``w_gev`` and
   ``w_true_gev``. For the single-nucleon channels their separation is Fermi
   smearing (<= 0.07 GeV in the median). For 2p2h it is not: there the two
   columns differ by about a nucleon mass because ``w_gev`` assumes one
   stationary nucleon while ``w_true_gev`` uses the 2N cluster, so the MEC curves
   are the sum of two channels' worth of target mass apart, not a smearing.
2. ``w_by_interaction`` — one panel per generator, ``w_true_gev`` split by
   channel, with each generator's own W thresholds drawn on its own panel:
   GENIE's tune-dependent ``Wcut``, NEUT's 1.3 / 2.0 GeV multi-pi window, NuWro's
   ``res_dis_cut``. GENIE's RES terminates exactly at ``Wcut``
   while its non-resonant background extends below it, so the two coexist on the
   low side and only RES stops at the line.

   The channel drawn is **not** simply the ``interaction`` column: every event
   whose ``resonant_primary`` says it was made non-resonantly is drawn in the
   ``dis`` bucket, which is why that bucket is labelled "dis + bkg". This only
   moves NuWro events (see ``_plot_channels``), and it is a plotting choice, not
   a change to the stored labels.
3. ``w_lepton_by_interaction`` — the same split on ``w_gev``. Worth having
   alongside: ``w_gev`` needs no struck nucleon, so it is the only W GiBUU's 2p2h
   has at all, and it is defined identically for every generator. The price is
   that it is Fermi-smeared relative to the W the generators actually cut on
   (and, for 2p2h, offset from ``w_true_gev`` by a whole nucleon mass rather than
   smeared), so no threshold lines are drawn on it.
4. ``w_across_generators`` — all four generators' ``w_gev`` on shared axes.
   This is the one that has to agree: ``w_gev`` is defined identically for every
   generator, so a disagreement here is physics, not bookkeeping.

Blanked events (placeholder ``MISSING``) are excluded from every histogram and
counted in the printed summary instead, since a placeholder is not a measurement.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import numpy as np

from neutrino_factory.common_output import RESONANT_PRIMARY_NO
from neutrino_factory.kinematics import MISSING
from neutrino_factory.plots import (
    INTERACTION_ORDER,
    _import_pyplot,
    _write_figure,
    dataset_legend_label,
    read_plot_data,
)

# Generator -> colour, fixed by identity and never cycled, so a generator keeps
# its colour however many are plotted. These are four steps of the Okabe-Ito
# qualitative palette, which is colourblind-safe by construction.
GENERATOR_COLORS = {
    "genie": "#0072B2",
    "nuwro": "#D55E00",
    "neut": "#009E73",
    "gibuu": "#CC79A7",
}
FALLBACK_COLOR = "#555555"

# Interaction -> colour, likewise fixed by identity. Keying on the channel rather
# than on its position in the legend matters here: GiBUU has no coherent channel,
# so an index-cycled palette would repaint `mec` with `coh`'s colour in that panel
# alone and make the four panels uncomparable.
INTERACTION_COLORS = {
    "qel": "#0072B2",
    "res": "#E69F00",
    "dis": "#009E73",
    "coh": "#CC79A7",
    "mec": "#56B4E9",
    "other": "#999999",
}

# The `dis` bucket is not pure deep-inelastic scattering in any of these
# generators -- it is where the non-resonant background lives too, by the
# taxonomy's own design. Naming it so on the axis stops the panels from being
# read as "DIS vs resonance".
CHANNEL_LABELS = {"dis": "dis + bkg"}

# GENIE's RES/DIS joining cut is **per tune**, not a single number: it is the
# `Wcut` parameter of the tune's `config/<tune>/CommonParam.xml`, which ranges
# from 1.7 (the global default) to 2.2802 across shipped tunes. Drawing the
# default on a tune that does not use it puts the line in the wrong place, which
# is exactly how this table came to exist. Values read from the GENIE source tree;
# a tune absent here gets no line rather than a guessed one.
GENIE_WCUT_BY_TUNE: dict[str, float] = {
    "G18_10a_02_11a": 1.927862,
    "G18_10a_02_11b": 1.809000,
    "AR23_20i_00_000": 1.809000,
}

# The other generators' thresholds are fixed. Keyed by generator, because a
# threshold drawn on another generator's panel invites reading it as a cut that
# generator makes -- GENIE has no 1.3/2.0 boundary and NEUT has no Wcut.
#
# NuWro's `res_dis_cut` is hardcoded here at its default, 1900 MeV -- read back
# and confirmed against `e/par/par.res_dis_cut` in this run's own output, but not
# read per run, because NuWro's per-event parameter branches do not survive into
# the merged HDF5 (see .claude/TODOS.md). It is the one genuinely hard boundary:
# the two dynamics are disjoint across it.
#
# `res_dis_blending_start` (1600) is deliberately NOT drawn. It is not where the
# non-resonant background begins -- `alfa.cc` ramps that fraction up from zero at
# W = 1080 MeV, reaches a channel-dependent base (0, 0.2 or 0.3) at 1600, and only
# then climbs to 1 at 1900. Drawing 1600 reads as an onset and is contradicted by
# the data, which has non-resonant events from 1.27 GeV up. `betadis` further
# replaces 1600 with `1300 - 75 * bkgrscaling` for the zero-base channels, so the
# parameter is not even a single number across channels.
THRESHOLDS: dict[str, tuple[tuple[float, str], ...]] = {
    "neut": ((1.3, "multi-$\\pi$ min"), (2.0, "multi-$\\pi$ max")),
    "nuwro": ((1.9, "res_dis_cut"),),
}


def _thresholds_for(data: dict) -> tuple[tuple[float, str], ...]:
    """The W thresholds that the generator producing ``data`` actually applies."""
    if data["generator"] == "genie":
        wcut = GENIE_WCUT_BY_TUNE.get(data["config_version"])
        return () if wcut is None else ((wcut, "$W_{cut}$"),)
    return THRESHOLDS.get(data["generator"], ())


W_BINS = np.linspace(0.8, 3.0, 89)

# Units in round parentheses, matching plots.XSEC_YLABEL and the rest of the
# project's axis labels.
W_YLABEL = (
    "d$\\sigma$/dW ($10^{-38} \\mathrm{cm}^2 / \\mathrm{nucleon} / \\mathrm{GeV}$)"
)


def _read(path: Path) -> dict:
    """Everything one figure needs from one merged file."""
    data = read_plot_data(path)
    with h5py.File(path, "r") as handle:
        events = handle["events"]
        w = np.asarray(events["w_gev"][:], dtype=np.float64)
        w_true = np.asarray(events["w_true_gev"][:], dtype=np.float64)
        resonant_primary = np.asarray(events["resonant_primary"][:], dtype=np.int64)
    return {
        "generator": str(data.metadata.get("generator", "unknown")),
        "config_version": str(data.metadata.get("config_version", "")),
        "label": dataset_legend_label(data),
        "xsec_weights": data.xsec_weights,
        "interactions": data.interactions,
        "resonant_primary": resonant_primary,
        "w_gev": w,
        "w_true_gev": w_true,
    }


def _plot_channels(data: dict) -> np.ndarray:
    """The channel each event is *drawn* in, which is not always its label.

    Every event the generator says was made non-resonantly is drawn in the
    ``dis`` bucket, whatever channel it was labelled. For GENIE and GiBUU this
    changes nothing -- their channels are their mechanisms -- and for NEUT nothing
    changes either, since it reports no mechanism at all. For NuWro it moves the
    non-resonant background out of ``res``, where its RES channel blends it in.

    This is a **plotting choice**, made here rather than in the normalizer: the
    common output keeps ``interaction`` exactly as each generator assigned it and
    records the mechanism separately in ``resonant_primary``, so this regrouping
    is reversible and costs no re-normalization. Whether it should become the
    stored labelling is an open decision (.claude/TODOS.md).
    """
    channels = np.array(data["interactions"], dtype=object).copy()
    channels[data["resonant_primary"] == RESONANT_PRIMARY_NO] = "dis"
    return channels


def _density(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Cross section per unit W, over the events where W is not a placeholder."""
    filled = values != MISSING
    counts, edges = np.histogram(values[filled], bins=W_BINS, weights=weights[filled])
    return counts / np.diff(edges)


def _draw_thresholds(ax, thresholds, label: bool = False) -> None:
    for position, name in thresholds:
        ax.axvline(position, color="black", linewidth=0.8, linestyle=":", zorder=0)
        if label:
            ax.text(
                position,
                ax.get_ylim()[1],
                f" {name}",
                rotation=90,
                va="top",
                ha="left",
                fontsize=7,
                color="#444444",
            )


def _panel_grid(datasets: list[dict], plt):
    columns = min(2, len(datasets))
    rows = -(-len(datasets) // columns)
    fig, axes = plt.subplots(rows, columns, figsize=(6.5 * columns, 4.0 * rows))
    return fig, np.atleast_1d(np.asarray(axes)).ravel()


def figure_definitions(datasets: list[dict], plt):
    """w_gev against w_true_gev, one panel per generator."""
    fig, axes = _panel_grid(datasets, plt)
    for ax, data in zip(axes, datasets):
        color = GENERATOR_COLORS.get(data["generator"], FALLBACK_COLOR)
        ax.stairs(
            _density(data["w_gev"], data["xsec_weights"]),
            W_BINS, fill=False, color=color, linewidth=2.0, label="$W$ (lepton-only)",
        )
        ax.stairs(
            _density(data["w_true_gev"], data["xsec_weights"]),
            W_BINS, fill=False, color=color, linewidth=2.0, linestyle="--",
            label="$W$ (struck system)",
        )
        ax.set_yscale("log")
        ax.set_title(data["label"], fontsize=10)
        ax.set_xlabel("W (GeV)")
        ax.set_ylabel(W_YLABEL)
        ax.legend(fontsize=8, frameon=False)
        _draw_thresholds(ax, _thresholds_for(data))
    for ax in axes[len(datasets):]:
        ax.set_visible(False)
    fig.suptitle(
        "Lepton-only W vs. W from the struck system "
        "(their gap is Fermi smearing — except for 2p2h, see below)"
    )
    return fig


def figure_by_interaction(
    datasets: list[dict],
    plt,
    column: str = "w_true_gev",
    xlabel: str = r"$W_{true}$ (GeV)",
    suptitle: str = "W by interaction channel, against the thresholds each generator applies",
    thresholds: bool = True,
):
    """``column`` split by interaction channel, one panel per generator.

    ``thresholds`` is off for the lepton-only column: the generators cut on their
    own internal W, which ``w_gev`` is a Fermi-smeared reconstruction of, so a cut
    line drawn there would sit next to an edge it does not produce.
    """
    fig, axes = _panel_grid(datasets, plt)
    for ax, data in zip(axes, datasets):
        # A channel whose W is blank for every event would contribute a legend
        # entry with nothing drawn against it: coherent in either column, and
        # GiBUU's 2p2h in w_true_gev only.
        channels = _plot_channels(data)
        present = [
            channel for channel in INTERACTION_ORDER
            if np.any((channels == channel) & (data[column] != MISSING))
        ]
        for channel in present:
            mask = channels == channel
            ax.stairs(
                _density(data[column][mask], data["xsec_weights"][mask]),
                W_BINS,
                fill=False,
                color=INTERACTION_COLORS.get(channel, FALLBACK_COLOR),
                linewidth=1.6,
                label=CHANNEL_LABELS.get(channel, channel),
            )
        # The quasi-elastic peak is an order of magnitude above the inelastic
        # continuum, which is the part these panels are about.
        ax.set_yscale("log")
        ax.set_title(data["label"], fontsize=10)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(W_YLABEL)
        ax.legend(fontsize=8, frameon=False, ncol=2)
        if thresholds:
            _draw_thresholds(ax, _thresholds_for(data), label=True)
    for ax in axes[len(datasets):]:
        ax.set_visible(False)
    fig.suptitle(suptitle)
    return fig


def figure_across_generators(datasets: list[dict], plt):
    """The cross-generator comparison: w_gev is defined identically everywhere."""
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for data in datasets:
        ax.stairs(
            _density(data["w_gev"], data["xsec_weights"]),
            W_BINS,
            fill=False,
            color=GENERATOR_COLORS.get(data["generator"], FALLBACK_COLOR),
            linewidth=2.0,
            label=data["label"],
        )
    ax.set_yscale("log")
    ax.set_xlabel("W (GeV), lepton-only")
    ax.set_ylabel(W_YLABEL)
    ax.legend(fontsize=9, frameon=False)
    # On shared axes each threshold still belongs to one generator, so it is
    # named after the one that owns it.
    overlay = tuple(
        (position, f"{data['generator'].upper()} {name}")
        for data in datasets
        for position, name in _thresholds_for(data)
    )
    _draw_thresholds(ax, overlay, label=True)
    ax.set_title("Lepton-only W across generators — one formula, so these should agree")
    return fig


def _summarize(datasets: list[dict]) -> None:
    print(f"{'generator':>10} {'events':>8} {'w blank':>9} {'w_true blank':>13} "
          f"{'median w':>9} {'median w_true':>14}")
    for data in datasets:
        w, w_true, weights = data["w_gev"], data["w_true_gev"], data["xsec_weights"]
        filled, filled_true = w != MISSING, w_true != MISSING
        print(
            f"{data['generator']:>10} {len(w):>8} "
            f"{(~filled).sum():>9} {(~filled_true).sum():>13} "
            f"{_weighted_median(w[filled], weights[filled]):>9.3f} "
            f"{_weighted_median(w_true[filled_true], weights[filled_true]):>14.3f}"
        )


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    if values.size == 0 or weights.sum() <= 0:
        return float("nan")
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cumulative, 0.5 * cumulative[-1])])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Merged common-output HDF5 files")
    parser.add_argument("--out-dir", required=True, help="Directory to write the PNGs to")
    args = parser.parse_args(argv)

    plt = _import_pyplot()
    datasets = [_read(Path(path)) for path in args.inputs]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = [
        _write_figure(figure_definitions(datasets, plt), out_dir / "w_definitions.png", plt),
        _write_figure(
            figure_by_interaction(datasets, plt), out_dir / "w_by_interaction.png", plt
        ),
        _write_figure(
            figure_by_interaction(
                datasets,
                plt,
                column="w_gev",
                xlabel="W (GeV), lepton-only",
                suptitle=(
                    "Lepton-only W by interaction channel — one definition for every "
                    "generator, and the only one GiBUU's 2p2h has"
                ),
                thresholds=False,
            ),
            out_dir / "w_lepton_by_interaction.png",
            plt,
        ),
        _write_figure(
            figure_across_generators(datasets, plt), out_dir / "w_across_generators.png", plt
        ),
    ]
    _summarize(datasets)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
