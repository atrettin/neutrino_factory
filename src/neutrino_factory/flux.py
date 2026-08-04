"""Framework-wide neutrino flux abstraction.

A neutrino flux is always a function of energy. Abstractly it is a *callable*
returning a number proportional to the flux density (arbitrary units) as a
function of the neutrino energy in GeV. This holds whether the flux is described
by a mathematical function (power law) or a precomputed histogram.

Every concrete flux knows its energy support ``[emin_gev, emax_gev]`` and the
neutrino ``particle`` it describes. ``to_histogram`` produces a binned
representation used both to hand the shape to generators and to draw synthetic
energies in stub mode (``sample_energies``).

Per-generator translation of this abstraction lives in the translators. GENIE
consumes it via a ROOT TH1 flux driver (``translators/genie.py``); NuWro and
GiBUU cannot take a continuous function, so they receive a finely-binned
``to_histogram`` spectrum (NuWro's ``beam_energy`` histogram string; GiBUU's
``nuExp=99`` user-flux file).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from .particles import PARTICLE_PDG


class FluxError(ValueError):
    """Raised when a flux configuration cannot be built or is invalid."""


class Flux(ABC):
    """A callable flux density vs. neutrino energy (GeV), in arbitrary units."""

    particle: str
    emin_gev: float
    emax_gev: float

    @abstractmethod
    def __call__(self, energy_gev: float) -> float:
        """Flux density at ``energy_gev`` (0.0 outside ``[emin_gev, emax_gev]``)."""
        raise NotImplementedError

    def to_histogram(
        self, nbins: int = 200, spacing: str = "linear"
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(bin_edges, bin_contents)`` sampling the flux over its support.

        ``bin_edges`` has ``nbins + 1`` entries; ``bin_contents`` has ``nbins``.
        ``spacing`` is ``"linear"`` or ``"log"``; log spacing resolves a steeply
        falling spectrum over a wide range at constant relative resolution, which
        linear spacing cannot (see the flux section of
        ``docs/generators/genie.md``). Subclasses with a native binning may
        override this.
        """
        if spacing == "linear":
            edges = np.linspace(self.emin_gev, self.emax_gev, nbins + 1)
        elif spacing == "log":
            if self.emin_gev <= 0.0:
                raise FluxError(
                    f"Log-spaced flux binning requires emin_gev > 0, got {self.emin_gev}"
                )
            edges = np.logspace(
                np.log10(self.emin_gev), np.log10(self.emax_gev), nbins + 1
            )
            # np.logspace round-trips through log10/power, so the endpoints can
            # drift by an ulp. Downstream consumers (GENIE's TH1 flux driver)
            # compare edges against the configured range and silently zero any
            # bin that falls outside it, so pin them exactly.
            edges[0] = self.emin_gev
            edges[-1] = self.emax_gev
        else:
            raise FluxError(f"Unknown flux binning spacing '{spacing}'. Use 'linear' or 'log'")
        centers = 0.5 * (edges[:-1] + edges[1:])
        contents = np.array([self(float(c)) for c in centers], dtype=np.float64)
        return edges, contents

    def sample_energies(self, n: int, rng: Any) -> list[float]:
        """Draw ``n`` energies distributed according to the flux shape.

        Inverse-CDF sampling over ``to_histogram``. ``rng`` is a
        ``random.Random`` (uses ``rng.random()``), so stub output is
        reproducible from the task seed.
        """
        edges, contents = self.to_histogram()
        widths = np.diff(edges)
        weights = np.clip(contents, 0.0, None) * widths
        total = float(weights.sum())
        if total <= 0.0:
            # Degenerate flux (no positive content) -> fall back to uniform.
            return [self.emin_gev + (self.emax_gev - self.emin_gev) * rng.random() for _ in range(n)]
        cdf = np.cumsum(weights) / total
        energies: list[float] = []
        for _ in range(n):
            u = rng.random()
            bin_index = int(np.searchsorted(cdf, u, side="right"))
            bin_index = min(bin_index, len(widths) - 1)
            # Uniform within the chosen bin.
            low, high = float(edges[bin_index]), float(edges[bin_index + 1])
            energies.append(low + (high - low) * rng.random())
        return energies


class PowerLawFlux(Flux):
    """Power-law spectrum: flux density proportional to ``E ** gamma``.

    ``gamma`` is the config value directly, so a falling ``E^-2`` spectrum is
    ``gamma = -2.0``.
    """

    def __init__(self, particle: str, emin_gev: float, emax_gev: float, gamma: float):
        self.particle = particle
        self.emin_gev = float(emin_gev)
        self.emax_gev = float(emax_gev)
        self.gamma = float(gamma)

    def __call__(self, energy_gev: float) -> float:
        if energy_gev < self.emin_gev or energy_gev > self.emax_gev:
            return 0.0
        return float(energy_gev) ** self.gamma


class HistogramFlux(Flux):
    """Precomputed histogram flux: bin contents are the density vs. energy (GeV)."""

    def __init__(self, particle: str, bin_edges: Any, bin_contents: Any):
        edges = np.asarray(bin_edges, dtype=np.float64)
        contents = np.asarray(bin_contents, dtype=np.float64)
        if edges.ndim != 1 or contents.ndim != 1 or len(edges) != len(contents) + 1:
            raise FluxError(
                "Histogram flux requires 1-D bin_edges of length len(bin_contents)+1"
            )
        self.particle = particle
        self.bin_edges = edges
        self.bin_contents = contents
        self.emin_gev = float(edges[0])
        self.emax_gev = float(edges[-1])
        # Set when loaded from a ROOT file so generators (e.g. GENIE) can hand
        # the same file to their native TH1 flux driver. None for in-memory histograms.
        self.source_path: Path | None = None
        self.source_name: str | None = None

    @classmethod
    def from_root_file(
        cls,
        path: str | Path,
        name: str,
        particle: str,
        contents_are_counts: bool = False,
    ) -> "HistogramFlux":
        """Load a TH1 histogram from a ROOT file via ``uproot``.

        ``contents_are_counts`` marks a histogram whose bin contents are already
        integrated over the bin (entry counts, or a density that has been
        multiplied by the bin width) rather than a density. Such contents are
        divided by the bin widths on load, so that a variable-width histogram
        yields the correct density. Required for GENIE's ``input-flux.root``,
        whose ``spectrum`` holds per-bin integrals.
        """
        try:
            import uproot
        except ImportError as exc:  # pragma: no cover - dependency always present in runtime
            raise FluxError(
                "uproot is required to read a histogram flux ROOT file. "
                "Install it with: pip install uproot"
            ) from exc

        root_path = Path(path)
        if not root_path.is_file():
            raise FluxError(f"Histogram flux file not found: {root_path}")
        with uproot.open(root_path) as handle:
            if name not in handle:
                available = ", ".join(sorted(handle.keys())) or "(none)"
                raise FluxError(
                    f"Histogram '{name}' not found in {root_path}. Available: {available}"
                )
            contents, edges = handle[name].to_numpy()
        if contents_are_counts:
            contents = np.asarray(contents, dtype=np.float64) / np.diff(edges)
        flux = cls(particle, edges, contents)
        flux.source_path = root_path.resolve()
        flux.source_name = name
        return flux

    def __call__(self, energy_gev: float) -> float:
        if energy_gev < self.emin_gev or energy_gev > self.emax_gev:
            return 0.0
        # Rightmost edge belongs to the last bin.
        index = int(np.searchsorted(self.bin_edges, energy_gev, side="right")) - 1
        index = min(max(index, 0), len(self.bin_contents) - 1)
        return float(self.bin_contents[index])


def _resolve_histogram_path(flux_config: dict[str, Any], base_dir: str | Path | None) -> Path:
    raw = str(flux_config.get("histogram_file") or "").strip()
    if not raw:
        raise FluxError("Histogram flux requires 'histogram_file'")
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    return path.resolve()


def build_flux(flux_config: dict[str, Any], base_dir: str | Path | None = None) -> Flux:
    """Construct a :class:`Flux` from a config ``flux`` block.

    ``base_dir`` resolves a relative ``histogram_file`` (typically the directory
    containing the config file).
    """
    flux_type = str(flux_config.get("type", "power_law"))
    particle = str(flux_config.get("particle", "numu"))

    if flux_type == "power_law":
        return PowerLawFlux(
            particle=particle,
            emin_gev=float(flux_config["emin_gev"]),
            emax_gev=float(flux_config["emax_gev"]),
            gamma=float(flux_config["gamma"]),
        )
    if flux_type == "histogram":
        path = _resolve_histogram_path(flux_config, base_dir)
        name = str(flux_config.get("histogram_name") or "").strip()
        if not name:
            raise FluxError("Histogram flux requires 'histogram_name'")
        return HistogramFlux.from_root_file(path, name, particle)

    raise FluxError(
        f"Unknown flux type '{flux_type}'. Supported: 'power_law', 'histogram'"
    )


def validate_flux(flux_config: dict[str, Any], base_dir: str | Path | None = None) -> list[str]:
    """Return a list of validation error strings (empty if valid)."""
    errors: list[str] = []
    flux_type = str(flux_config.get("type", "power_law"))

    # The probe is flux metadata here, but it selects the generator-native beam
    # setting in every translator; catching an unsupported flavour at
    # validate-config time beats a KeyError once tasks are already running.
    particle = str(flux_config.get("particle", "numu"))
    if particle not in PARTICLE_PDG:
        errors.append(
            f"Unknown flux.particle '{particle}'. Supported: {', '.join(PARTICLE_PDG)}"
        )

    if flux_type == "power_law":
        try:
            emin = float(flux_config.get("emin_gev", 0.0))
            emax = float(flux_config.get("emax_gev", 0.0))
        except (TypeError, ValueError):
            errors.append("flux energy bounds must be numeric")
            return errors
        if emin <= 0 or emax <= 0:
            errors.append("flux energy bounds must be positive")
        if emax < emin:
            errors.append("flux.emax_gev must be >= flux.emin_gev")
        try:
            float(flux_config["gamma"])
        except (KeyError, TypeError, ValueError):
            errors.append("power_law flux requires a numeric flux.gamma")
        return errors

    if flux_type == "histogram":
        try:
            flux = build_flux(flux_config, base_dir=base_dir)
        except FluxError as exc:
            errors.append(str(exc))
            return errors
        assert isinstance(flux, HistogramFlux)
        edges = flux.bin_edges
        contents = flux.bin_contents
        if len(contents) < 1:
            errors.append("histogram flux must have at least one bin")
        if np.any(edges <= 0):
            errors.append("histogram flux bin edges must be positive")
        if np.any(np.diff(edges) <= 0):
            errors.append("histogram flux bin edges must be strictly increasing")
        if np.any(contents < 0):
            errors.append("histogram flux bin contents must be non-negative")
        if not np.any(contents > 0):
            errors.append("histogram flux must have at least one positive bin")
        return errors

    errors.append(
        f"Unknown flux.type '{flux_type}'. Supported: 'power_law', 'histogram'"
    )
    return errors
