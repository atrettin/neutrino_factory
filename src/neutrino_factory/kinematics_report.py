"""Console report of the kinematic content of common-output HDF5 files.

Answers three questions about a normalized file: how many events it holds and how
they split by interaction type, how much statistical power those events actually
carry once cross-section weights are applied, and what the kinematic variables
look like per interaction type.

Everything distributional here is **weighted by ``xsec_weight``**. That is not a
refinement, it is a correctness requirement: GiBUU samples phase space uniformly
and weights by cross section, so its raw event distribution is not the physical
one. The weight-efficiency table exists to make that visible -- see
``docs/generators/gibuu.md`` on GiBUU weighting.

Following ``validate_output``, the diagnostic functions (``analyze_file``,
``analyze_config``) never print; they return plain dictionaries. The ``format_*``
helpers and the ``__main__`` block turn those into console output::

    python -m neutrino_factory.kinematics_report output/merged/run_genie_ver.h5

or, through the CLI::

    neutrino-factory analyze-kinematics --input output/merged/run_genie_ver.h5
    neutrino-factory analyze-kinematics --config configs/examples/power_law_numu_Ar.yaml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .common_output import read_events
from .kinematics import FIELD_DEFAULTS, KINEMATIC_FIELDS
from .plots import INTERACTION_ORDER
from .validate_output import expected_outputs

# Variables reported in the per-interaction tables, with the unit shown in the
# heading. energy_gev leads: it is the one kinematic column that predates the
# derived ones and has no placeholder.
REPORT_VARIABLES: tuple[tuple[str, str], ...] = (
    ("energy_gev", "GeV"),
    ("q2_gev2", "GeV^2"),
    ("bjorken_x", ""),
    ("inelasticity_y", ""),
    ("lepton_energy_gev", "GeV"),
    ("lepton_momentum_gev", "GeV"),
    ("lepton_p_parallel_gev", "GeV"),
    ("lepton_p_transverse_gev", "GeV"),
    ("lepton_costheta", ""),
)

ALL_LABEL = "all"


def _interaction_labels(interactions: np.ndarray) -> list[str]:
    """Canonical ordering, with any unexpected label appended rather than dropped."""
    seen = set(interactions.tolist())
    ordered = [label for label in INTERACTION_ORDER if label in seen]
    ordered += sorted(seen - set(INTERACTION_ORDER))
    return ordered


def kish_effective_size(weights: np.ndarray) -> float:
    """Kish effective sample size, ``(sum w)^2 / sum w^2``.

    The number of unweighted events that would carry the same statistical power.
    For a rejection-sampled generator this lands near the raw count; for GiBUU's
    QE channel it can be smaller by three orders of magnitude.
    """
    total = float(np.sum(weights))
    if weights.size == 0 or total == 0.0:
        return 0.0
    return float(total**2 / np.sum(weights**2))


def weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    """Weighted quantile that reduces to the ordinary one for uniform weights.

    The cumulative weight is evaluated at the *midpoint* of each point's weight
    rather than its upper edge; using the upper edge biases the result low by
    half a bin (the plain cumulative sum puts the median of 0..100 at 49.5).
    """
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights)
    total = cumulative[-1]
    if total <= 0.0:
        return float("nan")
    positions = (cumulative - 0.5 * weights) / total
    return float(np.interp(quantile, positions, values))


def _top_weight_share(weights: np.ndarray, fraction: float = 0.01) -> float:
    """Share of the total weight carried by the heaviest ``fraction`` of events."""
    total = float(np.sum(weights))
    if weights.size == 0 or total == 0.0:
        return 0.0
    count = max(1, int(fraction * weights.size))
    return float(np.sum(np.sort(weights)[::-1][:count]) / total)


def _variable_stats(
    values: np.ndarray, weights: np.ndarray, field: str
) -> dict[str, Any] | None:
    """Weighted mean/median and raw range for one variable, placeholders removed.

    Returns ``None`` when no event has a real value (e.g. ``bjorken_x`` for a
    coherent-only selection), which the formatter renders as a dash.
    """
    placeholder = FIELD_DEFAULTS.get(field)
    if placeholder is None:
        usable = np.isfinite(values)
    else:
        usable = np.isfinite(values) & (values != placeholder)

    blank = int(np.sum(~usable))
    values, weights = values[usable], weights[usable]
    if values.size == 0:
        return {"count": 0, "blank": blank, "mean": None, "median": None,
                "min": None, "max": None}

    weight_total = float(np.sum(weights))
    mean = float(np.sum(weights * values) / weight_total) if weight_total else float("nan")
    return {
        "count": int(values.size),
        "blank": blank,
        "mean": mean,
        "median": weighted_quantile(values, weights, 0.5),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def analyze_file(path: str | Path) -> dict[str, Any]:
    """Analyze one common-output HDF5 file. Returns a diagnostics dict.

    Never raises for a missing or unreadable file; those come back as
    ``ok = False`` with the reason in ``error``.
    """
    path = Path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "ok": False,
        "error": None,
        "generator": None,
        "code_version": None,
        "config_version": None,
        "probe": None,
        "target": None,
        "event_count": 0,
        "expected_events": None,
        "interactions": [],
        "weights": [],
        "variables": {},
    }

    if not path.is_file():
        result["error"] = "File does not exist"
        return result
    try:
        metadata, events = read_events(path)
    except (OSError, KeyError) as error:
        result["error"] = f"Could not read file: {error}"
        return result

    result["generator"] = str(metadata.get("generator", "unknown"))
    result["code_version"] = str(metadata.get("code_version", "unknown"))
    result["config_version"] = str(metadata.get("config_version", "unknown"))
    expected = metadata.get("expected_events")
    result["expected_events"] = int(expected) if expected is not None else None
    result["event_count"] = len(events)
    result["ok"] = True
    if not events:
        return result

    # The initial state as the events themselves report it. "mixed" when a file
    # disagrees with itself — worth seeing rather than silently picking one.
    for field in ("probe", "target"):
        values = {str(event[field]) for event in events}
        result[field] = values.pop() if len(values) == 1 else "mixed"

    interactions = np.array([event["interaction"] for event in events])
    xsec_weights = np.array([event["xsec_weight"] for event in events], dtype=np.float64)
    columns = {
        field: np.array([event[field] for event in events], dtype=np.float64)
        for field, _ in REPORT_VARIABLES
    }

    labels = _interaction_labels(interactions)
    selections: list[tuple[str, np.ndarray]] = [
        (ALL_LABEL, np.ones(len(events), dtype=bool))
    ] + [(label, interactions == label) for label in labels]

    for label, mask in selections:
        count = int(np.sum(mask))
        weights = xsec_weights[mask]
        if label != ALL_LABEL:
            result["interactions"].append({
                "interaction": label,
                "count": count,
                "share": count / len(events),
            })
        # Negative interference weights (GiBUU) make a signed sum meaningful but
        # break the Kish ratio, so efficiency is computed on |w|.
        magnitudes = np.abs(weights)
        result["weights"].append({
            "interaction": label,
            "count": count,
            "n_eff": kish_effective_size(magnitudes),
            "efficiency": kish_effective_size(magnitudes) / count if count else 0.0,
            "top1pct_share": _top_weight_share(magnitudes),
            "weight_sum": float(np.sum(weights)),
        })

    for field, _unit in REPORT_VARIABLES:
        rows = []
        for label, mask in selections:
            stats = _variable_stats(columns[field][mask], xsec_weights[mask], field)
            if stats is not None:
                rows.append({"interaction": label, **stats})
        result["variables"][field] = rows

    return result


def analyze_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Analyze every merged file a run configuration is expected to produce.

    Falls back to the expected entry's generator/version for a file that cannot
    be read, so a missing output is still reported under a meaningful heading.
    The probe and target always come from the configuration, which is what the
    run was asked to produce.
    """
    analyses = []
    for entry in expected_outputs(config)["merged"]:
        analysis = analyze_file(entry["path"])
        analysis["expected_events"] = (
            analysis["expected_events"]
            if analysis["expected_events"] is not None
            else int(entry["expected_events"])
        )
        analysis["probe"] = entry["particle"]
        analysis["target"] = entry["nucleus"]
        if not analysis["ok"]:
            analysis["generator"] = entry["generator"]
            version_id = str(entry["version_id"])
            code_version, _, config_version = version_id.partition("+")
            analysis["code_version"] = code_version
            analysis["config_version"] = config_version or "unknown"
        analyses.append(analysis)
    return analyses


def _format_number(value: float | None, width: int = 9, decimals: int = 3) -> str:
    if value is None:
        return "-".rjust(width)
    if not np.isfinite(value):
        return "nan".rjust(width)
    if value != 0.0 and (abs(value) >= 10**6 or abs(value) < 10**-3):
        return f"{value:>{width}.2e}"
    return f"{value:>{width}.{decimals}f}"


def _render_table(header: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "    ") -> list[str]:
    if not rows:
        return []
    widths = [
        max(len(str(header[col])), *(len(str(row[col])) for row in rows))
        for col in range(len(header))
    ]
    # The label column reads better left-aligned; the numeric columns right.
    def render(cells: Sequence[str]) -> str:
        rendered = [str(cells[0]).ljust(widths[0])]
        rendered += [str(cells[col]).rjust(widths[col]) for col in range(1, len(header))]
        return indent + "  ".join(rendered)

    return [render(header)] + [render(row) for row in rows]


def format_report(analysis: dict[str, Any]) -> str:
    """Render one ``analyze_file`` result as a human-readable block."""
    title = (
        f"{analysis['generator']}  {analysis['code_version']} + {analysis['config_version']}"
    )
    probe, target = analysis.get("probe"), analysis.get("target")
    if probe and target:
        title = f"{probe} on {target} — {title}"
    lines = ["=" * 78, title, f"  file: {analysis['path']}", "=" * 78]

    if not analysis["ok"]:
        lines.append(f"  ERROR: {analysis['error']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("Events")
    total = analysis["event_count"]
    expected = analysis["expected_events"]
    if expected:
        lines.append(
            f"    total {total} of {expected} expected ({total / expected:.1%})"
        )
    else:
        lines.append(f"    total {total}")

    if total == 0:
        lines.append("    (no events; nothing further to report)")
        return "\n".join(lines)

    lines += _render_table(
        ("interaction", "count", "share"),
        [
            (row["interaction"], str(row["count"]), f"{row['share']:.1%}")
            for row in analysis["interactions"]
        ],
    )

    lines.append("")
    lines.append("Weight efficiency  (Kish n_eff = (sum w)^2 / sum w^2, on |xsec_weight|)")
    lines += _render_table(
        ("interaction", "count", "n_eff", "n_eff/n", "top 1% of w", "sum w"),
        [
            (
                row["interaction"],
                str(row["count"]),
                _format_number(row["n_eff"], width=10, decimals=1),
                f"{row['efficiency']:.1%}",
                f"{row['top1pct_share']:.1%}",
                _format_number(row["weight_sum"], width=11, decimals=4),
            )
            for row in analysis["weights"]
        ],
    )
    lines.append(
        "    n_eff/n is the fraction of statistical power the raw event count "
        "actually carries."
    )

    lines.append("")
    lines.append(
        "Kinematics  (mean/median weighted by xsec_weight; min/max are raw; "
        "placeholders excluded)"
    )
    for field, unit in REPORT_VARIABLES:
        rows = analysis["variables"].get(field, [])
        if not rows:
            continue
        lines.append("")
        lines.append(f"  {field}" + (f"  [{unit}]" if unit else ""))
        lines += _render_table(
            ("interaction", "n", "blank", "mean", "median", "min", "max"),
            [
                (
                    row["interaction"],
                    str(row["count"]),
                    str(row["blank"]) if row["blank"] else "-",
                    _format_number(row["mean"]),
                    _format_number(row["median"]),
                    _format_number(row["min"]),
                    _format_number(row["max"]),
                )
                for row in rows
            ],
        )

    return "\n".join(lines)


def format_reports(analyses: Sequence[dict[str, Any]]) -> str:
    return "\n\n".join(format_report(analysis) for analysis in analyses)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neutrino_factory.kinematics_report",
        description="Report the kinematic content of common-output HDF5 files.",
    )
    parser.add_argument("files", nargs="+", help="HDF5 file(s) to analyze")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    analyses = [analyze_file(path) for path in args.files]
    if args.json:
        print(json.dumps({"analyses": analyses}, indent=2))
    else:
        print(format_reports(analyses))
    return 0 if all(analysis["ok"] for analysis in analyses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
