from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from neutrino_factory.common_output import write_common_hdf5
from neutrino_factory.plots import (
    CHANNEL_PANELS,
    _channel_mask,
    currents_present,
    dataset_legend_label,
    dataset_title,
    make_config_plots,
    read_plot_data,
)
from neutrino_factory.validate_output import expected_outputs


def _write(
    path: Path,
    *,
    generator: str = "genie",
    code_version: str = "R-3_06_00",
    config_version: str = "G18_10a_02_11a",
    currents: tuple[bool, ...] = (True, False),
    interactions: tuple[str, ...] = ("qel", "res"),
) -> str:
    metadata = {
        "generator": generator,
        "code_version": code_version,
        "config_version": config_version,
        "generator_version_id": f"{code_version}+{config_version}",
        "run_name": "unit_run",
        "chunk_id": 0,
        "seed": 1,
        "expected_events": len(currents),
    }
    events = [
        {
            "event_id": i,
            "seed": 1,
            "energy_gev": 1.0 + i,
            "weight": 1.0,
            "xsec_weight": 1.0,
            "is_cc": is_cc,
            "interaction": interactions[i % len(interactions)],
            "probe": "numu",
            "target": "C12",
            "generator": generator,
        }
        for i, is_cc in enumerate(currents)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_common_hdf5(path, metadata, events)


class CurrentsPresentTests(unittest.TestCase):
    """Which panels a dataset gets is decided from the data, not the config."""

    def test_cc_only(self) -> None:
        self.assertEqual(currents_present(np.array([True, True])), ("cc",))

    def test_nc_only(self) -> None:
        self.assertEqual(currents_present(np.array([False, False])), ("nc",))

    def test_inclusive(self) -> None:
        self.assertEqual(currents_present(np.array([True, False])), ("cc", "nc"))

    def test_no_events(self) -> None:
        self.assertEqual(currents_present(np.array([], dtype=bool)), ())


class ChannelMaskTests(unittest.TestCase):
    def test_named_channel_selects_only_its_label(self) -> None:
        interactions = np.array(["qel", "res", "dis"], dtype=object)
        self.assertEqual(_channel_mask(interactions, "res").tolist(), [False, True, False])

    def test_other_collects_unnamed_labels(self) -> None:
        # "inclusive" is stub mode's label and has no panel of its own.
        interactions = np.array(["qel", "other", "inclusive"], dtype=object)
        self.assertEqual(_channel_mask(interactions, "other").tolist(), [False, True, True])

    def test_every_event_lands_in_exactly_one_panel(self) -> None:
        interactions = np.array(["qel", "res", "mec", "dis", "coh", "other", "weird"], dtype=object)
        hits = np.sum(
            [_channel_mask(interactions, channel) for channel, _ in CHANNEL_PANELS], axis=0
        )
        self.assertEqual(hits.tolist(), [1] * len(interactions))


class DatasetLabelTests(unittest.TestCase):
    def test_title_and_legend_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.h5"
            _write(path)
            data = read_plot_data(path)

            self.assertEqual(
                dataset_title(data), "numu on C12 — genie R-3_06_00 / G18_10a_02_11a"
            )
            self.assertEqual(
                dataset_legend_label(data), "genie R-3_06_00+G18_10a_02_11a"
            )


class MakeConfigPlotsTests(unittest.TestCase):
    def _config(self, root: Path) -> dict:
        return {
            "run": {"name": "unit_run", "events": 4, "seed": 1},
            "flux": {
                "type": "power_law",
                "particle": "numu",
                "emin_gev": 0.5,
                "emax_gev": 5.0,
                "gamma": 0.0,
            },
            "target": {"nucleus": "C12", "pdg": 1000060120},
            "physics": {"mode": "inclusive", "current": "inclusive"},
            "generators": {
                "genie": {
                    "versions": [
                        {
                            "enabled": True,
                            "code_version": "R-3_06_00",
                            "config_version": "G18_10a_02_11a",
                        }
                    ]
                },
                "nuwro": {
                    "versions": [
                        {
                            "enabled": True,
                            "code_version": "21.09.2",
                            "config_version": "default",
                        }
                    ]
                },
            },
            "splitting": {"strategy": "events", "chunks": 1},
            "storage": {
                "software_root": str(root / "software"),
                "output_root": str(root),
                "work_root": str(root / "work"),
            },
        }

    def test_raises_when_no_merged_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError) as ctx:
                make_config_plots(self._config(Path(tmpdir)))
            self.assertIn("merged outputs", str(ctx.exception))

    def test_skips_missing_and_plots_what_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            merged = expected_outputs(config)["merged"]
            self.assertEqual(len(merged), 2)
            # Only the first generator's output exists; the other is skipped.
            present = Path(merged[0]["path"])
            _write(present)

            written = make_config_plots(config, bins=5)

            names = {Path(p).name for p in written}
            self.assertEqual(
                names,
                {
                    f"{present.stem}_interactions.png",
                    f"{present.stem}_energy.png",
                    f"{present.stem}_energy_weighted.png",
                    f"{present.stem}_xsec_by_type.png",
                    # The dataset is inclusive, so both comparison figures appear.
                    "unit_run_comparison_cc.png",
                    "unit_run_comparison_nc.png",
                },
            )
            # Default destination is <output_root>/plots.
            for path in written:
                self.assertEqual(Path(path).parent, root / "plots")
                self.assertTrue(Path(path).exists())

    def test_single_current_run_gets_one_comparison_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            for entry in expected_outputs(config)["merged"]:
                _write(Path(entry["path"]), currents=(True, True))

            written = make_config_plots(config, output_dir=root / "png", bins=5)

            comparisons = sorted(
                Path(p).name for p in written if "_comparison_" in Path(p).name
            )
            self.assertEqual(comparisons, ["unit_run_comparison_cc.png"])


if __name__ == "__main__":
    unittest.main()
