from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from neutrino_factory.common_output import write_common_hdf5
from neutrino_factory.config import resolve_config
from neutrino_factory.kinematics import FIELD_DEFAULTS, MISSING
from neutrino_factory.kinematics_report import (
    analyze_config,
    analyze_file,
    format_report,
    kish_effective_size,
    weighted_quantile,
)


def _event(index: int, interaction: str, *, xsec_weight: float = 1.0, **fields) -> dict:
    event = {
        "event_id": index,
        "seed": 1,
        "energy_gev": 1.0 + index,
        "weight": 1.0,
        "is_cc": True,
        "xsec_weight": xsec_weight,
        "interaction": interaction,
        "probe": "numu",
        "target": "C12",
        "generator": "genie",
    }
    event.update(fields)
    return event


def _write(path: Path, events: list[dict], **metadata) -> str:
    meta = {
        "generator": "genie",
        "code_version": "R-3_06_00",
        "config_version": "G18_10a_02_11a",
        "expected_events": len(events),
    }
    meta.update(metadata)
    return write_common_hdf5(path, meta, events)


class HelperTests(unittest.TestCase):
    def test_kish_equals_count_for_uniform_weights(self) -> None:
        self.assertAlmostEqual(kish_effective_size(np.ones(50)), 50.0)

    def test_kish_collapses_when_one_event_dominates(self) -> None:
        weights = np.array([1000.0] + [1e-6] * 999)
        self.assertLess(kish_effective_size(weights), 1.01)

    def test_kish_handles_empty_and_zero_weights(self) -> None:
        self.assertEqual(kish_effective_size(np.array([])), 0.0)
        self.assertEqual(kish_effective_size(np.zeros(10)), 0.0)

    def test_weighted_quantile_matches_median_for_uniform_weights(self) -> None:
        values = np.arange(101, dtype=np.float64)
        self.assertAlmostEqual(
            weighted_quantile(values, np.ones(101), 0.5), 50.0, places=6
        )

    def test_weighted_quantile_follows_the_weight(self) -> None:
        values = np.array([0.0, 10.0])
        # Nearly all the weight on the high value drags the median up to it.
        self.assertGreater(weighted_quantile(values, np.array([1e-6, 1.0]), 0.5), 9.9)


class AnalyzeFileTests(unittest.TestCase):
    def test_reports_version_identity_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            _write(path, [_event(i, "qel") for i in range(3)] + [_event(3, "dis")])

            analysis = analyze_file(path)

            self.assertTrue(analysis["ok"])
            self.assertEqual(analysis["generator"], "genie")
            self.assertEqual(analysis["code_version"], "R-3_06_00")
            self.assertEqual(analysis["config_version"], "G18_10a_02_11a")
            self.assertEqual(analysis["event_count"], 4)
            shares = {row["interaction"]: row["count"] for row in analysis["interactions"]}
            self.assertEqual(shares, {"qel": 3, "dis": 1})

    def test_weight_efficiency_is_per_interaction_and_overall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            # qel weights are uniform (efficient); dis is dominated by one event.
            events = [_event(i, "qel", xsec_weight=1.0) for i in range(10)]
            events += [_event(10 + i, "dis", xsec_weight=1e-6) for i in range(9)]
            events += [_event(19, "dis", xsec_weight=1000.0)]
            _write(path, events)

            rows = {row["interaction"]: row for row in analyze_file(path)["weights"]}

            self.assertAlmostEqual(rows["qel"]["n_eff"], 10.0)
            self.assertAlmostEqual(rows["qel"]["efficiency"], 1.0)
            self.assertLess(rows["dis"]["efficiency"], 0.2)
            self.assertGreater(rows["dis"]["top1pct_share"], 0.9)
            self.assertEqual(rows["all"]["count"], 20)

    def test_efficiency_uses_weight_magnitude_so_negative_weights_do_not_inflate_it(self) -> None:
        """GiBUU emits negative interference weights; |w| keeps the Kish ratio sane."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            events = [
                _event(i, "res", xsec_weight=1.0 if i % 2 == 0 else -1.0) for i in range(10)
            ]
            _write(path, events)

            rows = {row["interaction"]: row for row in analyze_file(path)["weights"]}

            # Signed sum is zero, but the sample still carries 10 events of power.
            self.assertAlmostEqual(rows["res"]["weight_sum"], 0.0)
            self.assertAlmostEqual(rows["res"]["n_eff"], 10.0)

    def test_statistics_are_weighted_by_xsec_weight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            events = [
                _event(0, "qel", xsec_weight=1.0, q2_gev2=1.0),
                _event(1, "qel", xsec_weight=3.0, q2_gev2=5.0),
            ]
            _write(path, events)

            rows = {r["interaction"]: r for r in analyze_file(path)["variables"]["q2_gev2"]}

            # Weighted mean is (1*1 + 3*5)/4 = 4.0, not the unweighted 3.0.
            self.assertAlmostEqual(rows["qel"]["mean"], 4.0)
            self.assertAlmostEqual(rows["qel"]["min"], 1.0)
            self.assertAlmostEqual(rows["qel"]["max"], 5.0)

    def test_placeholders_are_excluded_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            events = [
                _event(0, "dis", bjorken_x=0.4),
                _event(1, "coh", bjorken_x=MISSING),
                _event(2, "coh", bjorken_x=MISSING),
            ]
            _write(path, events)

            rows = {r["interaction"]: r for r in analyze_file(path)["variables"]["bjorken_x"]}

            self.assertEqual(rows["dis"]["count"], 1)
            self.assertEqual(rows["dis"]["blank"], 0)
            self.assertAlmostEqual(rows["dis"]["mean"], 0.4)
            # A coherent-only selection has nothing left to average.
            self.assertEqual(rows["coh"]["count"], 0)
            self.assertEqual(rows["coh"]["blank"], 2)
            self.assertIsNone(rows["coh"]["mean"])
            self.assertEqual(rows["all"]["blank"], 2)

    def test_signed_placeholder_is_excluded_without_touching_real_negatives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            events = [
                # A genuine backward scatter at cos(theta) = -1 must be kept.
                _event(0, "qel", lepton_costheta=-1.0),
                _event(1, "qel", lepton_costheta=FIELD_DEFAULTS["lepton_costheta"]),
            ]
            _write(path, events)

            rows = {
                r["interaction"]: r
                for r in analyze_file(path)["variables"]["lepton_costheta"]
            }

            self.assertEqual(rows["qel"]["count"], 1)
            self.assertEqual(rows["qel"]["blank"], 1)
            self.assertAlmostEqual(rows["qel"]["min"], -1.0)

    def test_missing_file_is_reported_not_raised(self) -> None:
        analysis = analyze_file(Path("/nonexistent/nope.h5"))
        self.assertFalse(analysis["ok"])
        self.assertIn("does not exist", analysis["error"])
        self.assertIn("ERROR", format_report(analysis))

    def test_empty_file_reports_zero_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            _write(path, [], expected_events=0)

            analysis = analyze_file(path)

            self.assertTrue(analysis["ok"])
            self.assertEqual(analysis["event_count"], 0)
            self.assertIn("no events", format_report(analysis))

    def test_unexpected_interaction_label_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            _write(path, [_event(0, "inclusive")])  # stub mode's label

            labels = [row["interaction"] for row in analyze_file(path)["interactions"]]

            self.assertEqual(labels, ["inclusive"])


class FormatReportTests(unittest.TestCase):
    def test_report_names_the_generator_and_every_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            _write(path, [_event(i, "qel") for i in range(4)])

            text = format_report(analyze_file(path))

            self.assertIn("genie", text)
            self.assertIn("R-3_06_00 + G18_10a_02_11a", text)
            self.assertIn("Weight efficiency", text)
            for field in ("energy_gev", "q2_gev2", "bjorken_x", "inelasticity_y",
                          "lepton_costheta", "lepton_p_parallel_gev"):
                self.assertIn(field, text)

    def test_report_columns_stay_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out.h5"
            # Mix a tiny and a huge value to exercise the scientific-notation path.
            _write(path, [
                _event(0, "qel", q2_gev2=1e-9),
                _event(1, "dis", q2_gev2=1e9),
            ])

            lines = format_report(analyze_file(path)).splitlines()
            block = lines[lines.index("  q2_gev2  [GeV^2]") + 1:]
            header, *rows = block[:4]
            for row in rows:
                self.assertEqual(len(row), len(header), msg=f"{row!r} vs {header!r}")


class AnalyzeConfigTests(unittest.TestCase):
    def _config(self, output_root: Path) -> dict:
        return resolve_config({
            "run": {"name": "unit_run", "seed": 1},
            "jobs": [{
                "generator": "genie",
                "code_version": "R-3_06_00",
                "config_version": "G18_10a_02_11a",
                "events": 4,
                "chunks": 1,
                "flux": {
                    "type": "power_law", "particle": "numu",
                    "emin_gev": 0.5, "emax_gev": 5.0, "gamma": 0.0,
                },
                "target": {"nucleus": "C12"},
                "physics": {"mode": "inclusive", "current": "cc"},
            }],
            "storage": {
                "software_root": str(output_root / "software"),
                "output_root": str(output_root),
                "work_root": str(output_root / "work"),
            },
        })

    def test_discovers_and_analyzes_the_merged_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            from neutrino_factory.validate_output import expected_outputs

            target = Path(expected_outputs(config)["merged"][0]["path"])
            _write(target, [_event(i, "qel") for i in range(4)])

            analyses = analyze_config(config)

            self.assertEqual(len(analyses), 1)
            self.assertTrue(analyses[0]["ok"])
            self.assertEqual(analyses[0]["generator"], "genie")
            self.assertEqual(analyses[0]["event_count"], 4)

    def test_missing_output_still_gets_a_meaningful_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            analyses = analyze_config(self._config(Path(tmpdir)))

            self.assertEqual(len(analyses), 1)
            analysis = analyses[0]
            self.assertFalse(analysis["ok"])
            # Identity comes from the config when the file cannot be read.
            self.assertEqual(analysis["generator"], "genie")
            self.assertEqual(analysis["code_version"], "R-3_06_00")
            self.assertEqual(analysis["config_version"], "G18_10a_02_11a")
            self.assertEqual(analysis["expected_events"], 4)
            self.assertEqual(analysis["probe"], "numu")
            self.assertEqual(analysis["target"], "C12")
            # The heading names the initial state, not just the generator.
            self.assertIn("numu on C12 \u2014 genie", format_report(analysis))


if __name__ == "__main__":
    unittest.main()
