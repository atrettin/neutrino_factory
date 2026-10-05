"""Reweight universes: parameter throws and the NuWro parameter tables.

A universe is one setting of a generator's physics parameters, drawn from
independent Gaussian priors. Its per-event weight is sigma_universe/sigma_central
at the event's kinematics, so an existing MC set can be re-evaluated under many
universes without being regenerated. Only NuWro is supported (see
docs/generators/nuwro.md for the reweighting mechanics, and docs/physics.md for
the sampling conventions).

Reproducibility is driven by an explicit ``seed`` rather than the run seed or
the job label: universe ``k`` must mean the same parameter values in every job
that uses the seed, or bin-to-bin correlations *between* jobs are lost. Each
parameter draws from its own stream, so adding a parameter leaves the others
unchanged, and raising ``count`` extends the existing universes instead of
redrawing them.
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np

# Parameters NuWro's QE reweighting engine handles correctly in nuwro_25.11,
# mapped to the ``qel_axial_ff_set`` values under which they have any effect
# (None: no condition). The axial mass only enters the dipole axial form factor;
# NuWro's default ``qel_axial_ff_set = 8`` (MINERvA) ignores it entirely, and its
# 1-sigma band is ``qel_minerva_ff_scale`` instead (central 0, range -1..+1).
# Closure against a directly generated sample is recorded in
# docs/generators/nuwro.md.
REWEIGHT_PARAMS: dict[str, frozenset[int] | None] = {
    "qel_cc_axial_mass": frozenset({1, 2, 3}),
    "qel_nc_axial_mass": frozenset({1, 2, 3}),
    "qel_s_axial_mass": None,
    "delta_s": None,
    "qel_minerva_ff_scale": frozenset({8}),
    "qel_deuterium_ff_scale": frozenset({9}),
}

# Flat per-channel factors with a central value of 1. NuWro lists them as
# reweightable, but its rewNorm engine never runs (upstream issue 1 in
# docs/generators/nuwro.md), so they are applied here, from the event flags.
NORM_PARAMS = (
    "qelNorm", "resNorm", "disNorm", "cohNorm", "mecNorm", "ccNorm", "ncNorm", "antyNorm",
)

# Physically positive parameters: a non-positive throw is an error, not a value.
POSITIVE_PARAMS = frozenset(
    {"qel_cc_axial_mass", "qel_nc_axial_mass", "qel_s_axial_mass", *NORM_PARAMS}
)

# Parameters NuWro accepts for reweighting but that do not work in nuwro_25.11.
BROKEN_PARAMS: dict[str, str] = {
    "qel_cc_vector_mass": "NuWro never reads it (the vector mass is hardcoded)",
    "SPPBkgScale": "NuWro never reads it",
    **{
        name: (
            "the RES engine is broken under the default hybrid model (res_kind = 2): "
            "res_angrew is never set, so every RES weight is NaN; and the hybrid "
            "model's generation does not depend on it either"
        )
        for name in ("pion_axial_mass", "pion_C5A")
    },
    **{
        name: "reweight_to keeps the value set at event 0, so later events get weight ~1"
        for name in (
            *(f"bba07_{p}{i}" for p in ("AEp", "AMp", "AEn", "AMn", "AAx") for i in range(1, 8)),
            "zexp_tc", "zexp_t0", *(f"zexp_a{i}" for i in range(10)),
            "qel_axial_2comp_gamma", "qel_axial_2comp_alpha",
            "qel_axial_3comp_theta", "qel_axial_3comp_beta",
        )
    },
    **{f"dynNorm{i}": "not supported; use the named channel norms" for i in range(10)},
}


def validate_universes(block: Any, where: str) -> list[str]:
    """Check a ``nuwro.universes`` block, returning error messages."""
    if not isinstance(block, dict):
        return [f"{where}: must be a mapping"]
    errors: list[str] = []
    seed = block.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        errors.append(
            f"{where}.seed: required non-negative integer (got {seed!r}); it is explicit "
            "so that universe k is the same in every job that uses it"
        )
    count = block.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        errors.append(f"{where}.count: must be an integer >= 1 (got {count!r})")
    parameters = block.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        return errors + [f"{where}.parameters: must be a non-empty mapping"]
    for name, spec in parameters.items():
        at = f"{where}.parameters.{name}"
        if name in BROKEN_PARAMS:
            errors.append(f"{at}: not supported — {BROKEN_PARAMS[name]}")
            continue
        if name not in REWEIGHT_PARAMS and name not in NORM_PARAMS:
            errors.append(
                f"{at}: unknown parameter. Supported: "
                f"{', '.join([*REWEIGHT_PARAMS, *NORM_PARAMS])}"
            )
            continue
        if not isinstance(spec, dict):
            errors.append(f"{at}: must be a mapping with 'sigma'")
            continue
        unknown = set(spec) - {"sigma", "log", "source"}
        if unknown:
            errors.append(
                f"{at}: unknown keys {sorted(unknown)} (central is taken from the "
                "generation, not the config)"
            )
        sigma = spec.get("sigma")
        if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or sigma <= 0:
            errors.append(f"{at}.sigma: must be a number > 0 (got {sigma!r})")
        if not isinstance(spec.get("log", False), bool):
            errors.append(f"{at}.log: must be true or false")
    return errors


def draw_z(seed: int, name: str, count: int) -> np.ndarray:
    """Standard-normal throws for one parameter, independent of all others."""
    rng = np.random.default_rng([seed, zlib.crc32(name.encode())])
    return rng.standard_normal(count)


def universe_values(name: str, central: float, sigma: float, log: bool, z: np.ndarray) -> np.ndarray:
    """Map throws onto parameter values.

    Linear: ``central + sigma*z``. Log: ``central*exp(sigma*z)``, so ``sigma`` is the
    standard deviation of ``ln(value)`` (the fractional error, for small sigma), the
    median is ``central``, and ``ln(value/central) = sigma*z`` exactly.
    """
    if log:
        if central <= 0:
            raise ValueError(
                f"Universe parameter {name}: log sampling needs a positive central "
                f"value, but generation used {central}"
            )
        return central * np.exp(sigma * z)
    values = central + sigma * z
    if name in POSITIVE_PARAMS and np.any(values <= 0):
        raise ValueError(
            f"Universe parameter {name}: {int(np.sum(values <= 0))} throw(s) are <= 0 "
            f"(central {central}, sigma {sigma}). Use 'log: true' for a positive "
            "parameter instead of a wide linear prior."
        )
    return values


def norm_factors(
    values: dict[str, np.ndarray], flags: dict[str, np.ndarray], antineutrino: bool
) -> np.ndarray:
    """The ``(n_events, count)`` product of the channel norm factors.

    ``flags`` holds per-event booleans ``qel res dis coh mec cc``; ``values`` the
    per-universe factor of each norm parameter present in the block.
    """
    n_events = len(flags["cc"])
    count = len(next(iter(values.values()))) if values else 0
    factors = np.ones((n_events, count))
    for name, factor in values.items():
        channel = name.removesuffix("Norm")
        if channel == "nc":
            mask = ~np.asarray(flags["cc"], dtype=bool)
        elif channel == "anty":
            mask = np.full(n_events, antineutrino)
        else:
            mask = np.asarray(flags[channel], dtype=bool)
        factors[mask] *= np.asarray(factor)[None, :]
    return factors


def resolve_universes(block: dict[str, Any], generation: dict[str, float]) -> dict[str, Any]:
    """Turn a validated block into the universes metadata, given the generation.

    ``generation`` holds the run's own parameter values (NuWro's ``e/par``): the
    central value of every reweighted parameter and ``qel_axial_ff_set``. Norm
    parameters have central 1. Raises if a parameter has no effect under the
    run's form-factor choice, rather than silently writing weights of 1.
    """
    seed, count = int(block["seed"]), int(block["count"])
    parameters: dict[str, dict[str, Any]] = {}
    z: dict[str, list[float]] = {}
    values: dict[str, list[float]] = {}
    ff_set = generation.get("qel_axial_ff_set")
    for name, spec in block["parameters"].items():
        required = REWEIGHT_PARAMS.get(name)
        if required is not None and int(ff_set if ff_set is not None else -1) not in required:
            raise ValueError(
                f"Universe parameter {name} has no effect: the run used "
                f"qel_axial_ff_set = {ff_set}, and it only acts under "
                f"{sorted(required)}. Under the MINERvA axial form factor (8) the QE "
                "knob is qel_minerva_ff_scale."
            )
        central = 1.0 if name in NORM_PARAMS else float(generation[name])
        sigma, log = float(spec["sigma"]), bool(spec.get("log", False))
        throws = draw_z(seed, name, count)
        parameters[name] = {
            "central": central, "sigma": sigma, "log": log, "source": spec.get("source", ""),
        }
        z[name] = throws.tolist()
        values[name] = universe_values(name, central, sigma, log, throws).tolist()
    return {"seed": seed, "count": count, "parameters": parameters, "z": z, "values": values}
