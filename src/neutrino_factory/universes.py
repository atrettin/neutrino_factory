"""Reweight universes and variations: parameter throws, tables and weight files.

A universe is one setting of a generator's physics parameters, drawn from
independent Gaussian priors. Its per-event weight is sigma_universe/sigma_central
at the event's kinematics, so an existing MC set can be re-evaluated under many
universes without being regenerated. A variation is one switch-type knob (an
interpolation between two models on [0, 1]) moved to its alternate setting:
it follows no Gaussian prior, so it gets its own weight column, never a
universe. The sampling here is generator-agnostic; only the parameter tables
and the binary that turns parameter values into weights are per generator
(NuWro: ``nf_reweight``, docs/generators/nuwro.md; GENIE: ``nf_genie_reweight``,
docs/generators/genie.md). The sampling conventions are in docs/physics.md.

Reproducibility is driven by an explicit ``seed`` rather than the run seed or
the job label: universe ``k`` must mean the same parameter values in every job
that uses the seed, or bin-to-bin correlations *between* jobs are lost. Each
parameter draws from its own stream, so adding a parameter leaves the others
unchanged, and raising ``count`` extends the existing universes instead of
redrawing them.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import numpy as np

# Files a generator adapter writes into the chunk's work dir for its normalizer.
SPEC_FILE = "universes.txt"  # input of the reweighting binary
RESOLVED_FILE = "universes.json"  # resolved universes/variations metadata
WEIGHTS_FILE = "universe_weights.root"  # output of the reweighting binary

# ── NuWro ────────────────────────────────────────────────────────────────────

# Parameters NuWro's QE reweighting engine handles correctly in nuwro_25.11,
# mapped to the ``qel_axial_ff_set`` values under which they have any effect
# (None: no condition). The axial mass only enters the dipole axial form factor;
# NuWro's default ``qel_axial_ff_set = 8`` (MINERvA) ignores it entirely, and its
# 1-sigma band is ``qel_minerva_ff_scale`` instead (central 0, range -1..+1).
# Closure against a directly generated sample is recorded in
# docs/generators/nuwro.md.
NUWRO_REWEIGHT_PARAMS: dict[str, frozenset[int] | None] = {
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
NUWRO_NORM_PARAMS = (
    "qelNorm", "resNorm", "disNorm", "cohNorm", "mecNorm", "ccNorm", "ncNorm", "antyNorm",
)

# Parameters NuWro accepts for reweighting but that do not work in nuwro_25.11.
NUWRO_BROKEN_PARAMS: dict[str, str] = {
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

# ── GENIE ────────────────────────────────────────────────────────────────────

# GENIE Reweight dials usable as Gaussian universe parameters, by their
# GSyst::AsString names, each mapped to what it scales. Every one is a positive
# multiplicative scale on the generation tune's own value (central 1):
# nf_genie_reweight overrides GENIE's 1-sigma table to 1, so GENIE's
# p = p_def * (1 + dial * err) becomes p = p_def * value. Each was checked on a
# 20k C12 sample of its validated CMC (GENIE_VALIDATED_CMC, or
# GENIE_DIAL_VALIDATED_CMC; docs/generators/genie.md): nominal weight exactly 1,
# finite weights, an effect on the expected channel only, and for MaCCQE closure
# against a directly generated sample. Each also needs the tune's model to be the
# one the engine handles; nf_genie_reweight fails loudly where it is not (e.g.
# MaCCQE under the z-expansion of AR23_20i).
GENIE_SCALE_DIALS: dict[str, str] = {
    "MaCCQE": "CCQE axial mass (dipole form factor; shape and normalization)",
    "NormCCQE": "CCQE normalization",
    "ZNormCCQE": "CCQE normalization (z-expansion axial form factor)",
    **{
        f"ZExpA{i}CCQE": f"CCQE z-expansion axial form factor coefficient a{i}"
        for i in range(1, 5)
    },
    "CoulombCCQE": "Coulomb potential scale in the Nieves CCQE model",
    "MaCCRES": "CC resonance axial mass (shape and normalization)",
    "MvCCRES": "CC resonance vector mass (shape and normalization)",
    "NormCCRES": "CC resonance normalization",
    "NonRESBGvpCC1pi": "non-resonant background, nu p CC 1pi (DIS events with W < 2 GeV)",
    "NonRESBGvnCC1pi": "non-resonant background, nu n CC 1pi (DIS events with W < 2 GeV)",
    "NonRESBGvpCC2pi": "non-resonant background, nu p CC 2pi (DIS events with W < 2 GeV)",
    "NonRESBGvnCC2pi": "non-resonant background, nu n CC 2pi (DIS events with W < 2 GeV)",
    **{
        f"NonRESBG{probe}{nuc}{cur}{npi}pi": (
            f"non-resonant background, {label} {nuc} {cur} {npi}pi (DIS events with W < 2 GeV)"
        )
        for probe, label in (("v", "nu"), ("vbar", "nubar"))
        for cur in ("CC", "NC")
        for npi in (1, 2)
        for nuc in ("p", "n")
        if (probe, cur) != ("v", "CC")
    },
    "AhtBY": "Bodek-Yang higher-twist A",
    "BhtBY": "Bodek-Yang higher-twist B",
    "CV1uBY": "Bodek-Yang valence-u correction CV1u",
    "CV2uBY": "Bodek-Yang valence-u correction CV2u",
    "MaCOHpi": "coherent pion production axial mass",
    "R0COHpi": "coherent pion production nuclear size parameter",
    "NormCCCOH": "CC coherent pion production normalization",
    "NormCCMEC": "CC MEC (2p2h) normalization",
    "MFP_pi": "pion mean free path in the nucleus (hA2018 FSI)",
    "MFP_N": "nucleon mean free path in the nucleus (hA2018 FSI)",
    "FrCEx_pi": "pion charge-exchange fate fraction (hA2018 FSI)",
    "FrInel_pi": "pion inelastic fate fraction (hA2018 FSI)",
    "FrAbs_pi": "pion absorption fate fraction (hA2018 FSI)",
    "FrPiProd_pi": "pion pion-production fate fraction (hA2018 FSI)",
    "FrCEx_N": "nucleon charge-exchange fate fraction (hA2018 FSI)",
    "FrInel_N": "nucleon inelastic fate fraction (hA2018 FSI)",
    "FrAbs_N": "nucleon absorption fate fraction (hA2018 FSI)",
    "FrPiProd_N": "nucleon pion-production fate fraction (hA2018 FSI)",
}

# Switch-type dials: interpolations between the tune's model (0) and an
# alternative (1), so they get a variation column, not a Gaussian. Mapped to the
# meaning of 1, read from the GENIE Reweight R-1_04_02 engine sources.
GENIE_SWITCH_DIALS: dict[str, str] = {
    "RPA_CCQE": "1 = Nieves CCQE with RPA off (0 = the tune's, RPA on)",
    "XSecShape_CCMEC": (
        "1 = CC MEC (Tl, cos theta_l) shape of the Empirical MEC model, at the tune's "
        "MEC total cross section"
    ),
    "DecayAngMEC": (
        "1 = MEC nucleon-cluster decay as 3 cos^2(theta) about q in the cluster frame "
        "(0 = isotropic, the tune's)"
    ),
    "Theta_Delta2Npi": (
        "1 = isotropic Delta -> N pi decay (0 = the Rein-Sehgal angular distribution "
        "the engine assumes for the tune)"
    ),
}

# Dials that do not work, or that were checked and rejected.
GENIE_BROKEN_PARAMS: dict[str, str] = {
    "FormZone": (
        "not cross-section preserving (+20% moves the mean DIS weight to 0.85) and the "
        "engine logs FATAL 'INTRANUKE didn't set a valid rescattering code' on ~1% of events"
    ),
    "AGKYxF1pi": "not cross-section preserving, with single weights up to ~2.4 at +20%",
    "AGKYpT1pi": "not cross-section preserving, with single weights up to ~5 at +20%",
    "DISNuclMod": "GENIE Reweight's engine exits for any non-zero value ('Not implemented')",
    "FrElas_pi": "GENIE Reweight R-1_04_02 does not include the elastic fate",
    "FrElas_N": "GENIE Reweight R-1_04_02 does not include the elastic fate",
}

# Dials that select incompatible engine modes when combined.
GENIE_EXCLUSIVE: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"MaCCQE"}), frozenset({"NormCCQE"})),
    (frozenset({"MaCCRES", "MvCCRES"}), frozenset({"NormCCRES"})),
)

# hA2018 fate groups: each needs one fate left as the "cushion" that absorbs
# unitarity, so at most three of the four may be varied.
GENIE_FATE_GROUPS = (
    ("FrCEx_pi", "FrInel_pi", "FrAbs_pi", "FrPiProd_pi"),
    ("FrCEx_N", "FrInel_N", "FrAbs_N", "FrPiProd_N"),
)

# ── GENIE tune compatibility ─────────────────────────────────────────────────

class GenieModels(NamedTuple):
    """The models of one GENIE comprehensive model configuration (CMC) that the
    Reweight engines take their defaults from: ``alg/config`` ids without the
    ``genie::`` prefix, from the CMC's ``ModelConfiguration.xml``."""

    fsi: str  # HadronTransp-Model
    qel_cc: str
    res_cc: str
    coh_cc_pion: str
    dis_cc: str
    mec_cc: str


# Every CMC of each code version that has nf_genie_reweight, by the CMC name
# that prefixes its tunes (G18_10a for G18_10a_02_11a). A tune's own directory
# only overrides parameters, never these models. Snapshot of the payload's
# config/<CMC>/ModelConfiguration.xml, checked against it by
# tests/test_universes.py where the payload is present. CMCs that leave any of
# these models unset (EM-only, HNL, dark matter, high-energy) are left out.
GENIE_TUNE_MODELS: dict[str, dict[str, GenieModels]] = {
    "R-3_06_00": {
        "AR23_20i": GenieModels(
            "HAIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G00_00a": GenieModels(
            "HAIntranuke/Default", "LwlynSmithQELCCPXSec/DipoleAndOldIntegrator",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G00_00b": GenieModels(
            "HAIntranuke/Default", "LwlynSmithQELCCPXSec/DipoleAndOldIntegrator",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G00_00z": GenieModels(
            "HAIntranuke/Default", "LwlynSmithQELCCPXSec/DipoleAndOldIntegrator",
            "ReinSehgalRESPXSec/WithPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_01a": GenieModels(
            "HAIntranuke2018/Default", "LwlynSmithQELCCPXSec/Dipole",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_01b": GenieModels(
            "HNIntranuke2018/Default", "LwlynSmithQELCCPXSec/Dipole",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_02a": GenieModels(
            "HAIntranuke2018/Default", "LwlynSmithQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_02b": GenieModels(
            "HNIntranuke2018/Default", "LwlynSmithQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_02c": GenieModels(
            "HINCLCascadeIntranuke/Default", "LwlynSmithQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_02d": GenieModels(
            "HG4BertCascIntranuke/Default", "LwlynSmithQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "G18_10a": GenieModels(
            "HAIntranuke2018/Default", "NievesQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G18_10b": GenieModels(
            "HNIntranuke2018/Default", "NievesQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G18_10c": GenieModels(
            "HINCLCascadeIntranuke/Default", "NievesQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G18_10d": GenieModels(
            "HG4BertCascIntranuke/Default", "NievesQELCCPXSec/Dipole",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G18_10i": GenieModels(
            "HAIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G18_10j": GenieModels(
            "HNIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "G21_11a": GenieModels(
            "HAIntranuke2018/Default", "HybridXSecAlgorithm/SuSAv2-QEL",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G21_11b": GenieModels(
            "HNIntranuke2018/Default", "HybridXSecAlgorithm/SuSAv2-QEL",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G21_11c": GenieModels(
            "HINCLCascadeIntranuke/Default", "HybridXSecAlgorithm/SuSAv2-QEL",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G21_11d": GenieModels(
            "HG4BertCascIntranuke/Default", "HybridXSecAlgorithm/SuSAv2-QEL",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G24_20i": GenieModels(
            "HAIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G24_20j": GenieModels(
            "HNIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G24_20k": GenieModels(
            "HINCLCascadeIntranuke/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "G24_20l": GenieModels(
            "HG4BertCascIntranuke/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
        "GDM18_00a": GenieModels(
            "HAIntranuke/Default", "LwlynSmithQELCCPXSec/Dipole",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "GDM18_00b": GenieModels(
            "HAIntranuke/Default", "LwlynSmithQELCCPXSec/Dipole",
            "ReinSehgalRESPXSec/NoPauliBlock", "ReinSehgalCOHPiPXSec/Default",
            "KNOTunedQPMDISPXSec/Default", "EmpiricalMECPXSec2015/Default",
        ),
        "GPRD18_10a": GenieModels(
            "HAIntranuke2018/Default", "XSecLinearCombinations/NievesQELCCPXSecNoRPA",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "NievesSimoVacasMECPXSec2016/Default",
        ),
        "N24_20i": GenieModels(
            "HAIntranuke2018/Default", "NievesQELCCPXSec/ZExp",
            "BergerSehgalRESPXSec2014/NoPauliBlock", "BergerSehgalCOHPiPXSec2015/Default",
            "KNOTunedQPMDISPXSec/Default", "SuSAv2MECPXSec/Default",
        ),
    },
}

# The CMC every dial in GENIE_SCALE_DIALS and GENIE_SWITCH_DIALS was checked on,
# except those in GENIE_DIAL_VALIDATED_CMC.
GENIE_VALIDATED_CMC = ("R-3_06_00", "G18_10a")

# Dials that cannot act under G18_10a's models, checked on another CMC: the
# z-expansion dials need a z-expansion axial form factor.
GENIE_ZEXP_DIALS = ("ZNormCCQE", *(f"ZExpA{i}CCQE" for i in range(1, 5)))
GENIE_DIAL_VALIDATED_CMC: dict[str, tuple[str, str]] = dict.fromkeys(
    GENIE_ZEXP_DIALS, ("R-3_06_00", "AR23_20i")
)

# The non-resonant background dials, each acting only on one probe (nu or nubar)
# and one current: GReWeightNonResonanceBkg picks the dial per event from
# GSyst::RBkg(interaction type, probe, hit nucleon, n_pi).
GENIE_NONRESBG_DIALS = tuple(name for name in GENIE_SCALE_DIALS if name.startswith("NonRESBG"))

# The model each dial's engine takes its defaults from, by GenieModels field.
GENIE_DIAL_MODEL: dict[str, str] = {
    **dict.fromkeys(("MaCCQE", "NormCCQE", "RPA_CCQE", "CoulombCCQE", *GENIE_ZEXP_DIALS), "qel_cc"),
    **dict.fromkeys(("MaCCRES", "MvCCRES", "NormCCRES", "Theta_Delta2Npi"), "res_cc"),
    **dict.fromkeys(("MaCOHpi", "R0COHpi", "NormCCCOH"), "coh_cc_pion"),
    # The NC non-resonant background dials too: every CMC uses the same DIS
    # model for CC and NC, and the snapshot records only the CC one.
    **dict.fromkeys((*GENIE_NONRESBG_DIALS, "AhtBY", "BhtBY", "CV1uBY", "CV2uBY"), "dis_cc"),
    **dict.fromkeys(("NormCCMEC", "XSecShape_CCMEC", "DecayAngMEC"), "mec_cc"),
    **dict.fromkeys(
        ("MFP_pi", "MFP_N", *GENIE_FATE_GROUPS[0], *GENIE_FATE_GROUPS[1]), "fsi"
    ),
}


def _genie_dial_problem(name: str, models: GenieModels) -> str | None:
    """Why a dial cannot work under a CMC's models, or None. From the GENIE
    Reweight R-1_04_02 engine sources."""
    qel_alg, _, qel_config = models.qel_cc.partition("/")
    if GENIE_DIAL_MODEL.get(name) == "fsi" and not models.fsi.startswith("HAIntranuke2018/"):
        # GReWeightINuke's constructor exits unless the FSI model is hA2018.
        return f"needs hA2018 FSI, but the tune uses {models.fsi}"
    if name in ("MaCCQE", "NormCCQE") and not (
        qel_alg in ("LwlynSmithQELCCPXSec", "NievesQELCCPXSec") and qel_config.startswith(("Dipole", "RunningMA"))
    ):
        # GReWeightNuXSecCCQE handles both only with a dipole (or running-MA)
        # axial form factor; the CMCs' QEL-CC configs name it.
        hint = "; use ZNormCCQE and ZExpA1CCQE-ZExpA4CCQE" if qel_config.startswith("ZExp") else ""
        return (
            f"needs a dipole axial form factor, but the tune's CCQE model is {models.qel_cc}{hint}"
        )
    if name in GENIE_ZEXP_DIALS and not (
        qel_alg in ("LwlynSmithQELCCPXSec", "NievesQELCCPXSec") and qel_config.startswith("ZExp")
    ):
        # The engine switches itself to its z-expansion mode only under a
        # z-expansion axial form factor; elsewhere it does not handle them.
        return (
            f"needs a z-expansion axial form factor, but the tune's CCQE model is {models.qel_cc}"
        )
    if name == "CoulombCCQE" and (qel_alg != "NievesQELCCPXSec" or "NoCoulomb" in qel_config):
        # The engine sets "CoulombScale" on a copy of the tune's CCQE model; only
        # the Nieves model reads it, and only with its Coulomb correction on.
        return (
            f"needs the Nieves CCQE model with its Coulomb correction, but the tune's is "
            f"{models.qel_cc}"
        )
    if name == "RPA_CCQE" and qel_alg != "NievesQELCCPXSec":
        # The engine switches the "RPA" flag of the tune's CCQE model, which
        # only the Nieves model has: elsewhere every weight would be 1.
        return f"needs the Nieves CCQE model, but the tune's is {models.qel_cc}"
    if name == "XSecShape_CCMEC" and models.mec_cc.startswith("EmpiricalMECPXSec2015/"):
        # The alternative shape is the Empirical MEC model's own.
        return "reshapes MEC towards the Empirical MEC model, which the tune already uses"
    return None


def validate_genie_tune(
    names: Iterable[str], code_version: str, tune: str, where: str
) -> tuple[list[str], list[str]]:
    """Check GENIE dials against the job's tune: (errors, warnings).

    An error is a dial that cannot work under the tune's models. A warning is a
    dial whose engine model differs from the one it was checked under.
    """
    names = [name for name in names if name in GENIE_DIAL_MODEL]
    if not names:
        return [], []
    by_cmc = GENIE_TUNE_MODELS.get(code_version)
    if by_cmc is None:
        return [
            f"{where}: GENIE universes and variations need nf_genie_reweight, which is "
            f"built and checked only for {', '.join(GENIE_TUNE_MODELS)} (got '{code_version}')"
        ], []
    cmc = "_".join(tune.split("_")[:2])
    models = by_cmc.get(cmc)
    if models is None:
        return [
            f"{where}: GENIE tune '{tune}' (model configuration '{cmc}') is not in the "
            f"reweighting model table for {code_version}; known: {', '.join(by_cmc)}"
        ], []
    errors: list[str] = []
    warnings: list[str] = []
    for name in names:
        problem = _genie_dial_problem(name, models)
        if problem:
            errors.append(f"{where}: GENIE dial '{name}' {problem}")
            continue
        field = GENIE_DIAL_MODEL[name]
        checked_version, checked_cmc = GENIE_DIAL_VALIDATED_CMC.get(name, GENIE_VALIDATED_CMC)
        validated = GENIE_TUNE_MODELS[checked_version][checked_cmc]
        if getattr(models, field) != getattr(validated, field):
            warnings.append(
                f"GENIE dial '{name}' under tune {cmc}* is unvalidated: it was checked with "
                f"{getattr(validated, field)} ({checked_cmc}), the tune uses "
                f"{getattr(models, field)}"
            )
    return errors, warnings


def genie_inert_dials(names: Iterable[str], antineutrino: bool, current: str) -> str | None:
    """A warning naming the non-resonant background dials that cannot act on a job, or None.

    Each acts only on its own probe (nu or nubar) and current, so in a job of the
    other flavor, or a CC- or NC-only job of the other current, its weights are 1.
    That is accepted, so that one block can serve every flavor and universe k
    stays the same throws everywhere; the message has no job index, so a matrix
    logs it once per flavor and current.
    """
    inert = []
    for name in names:
        if name not in GENIE_NONRESBG_DIALS:
            continue
        rest = name.removeprefix("NonRESBG")
        dial_nubar = rest.startswith("vbar")
        dial_current = "nc" if "NC" in rest else "cc"
        if dial_nubar != antineutrino or current not in ("inclusive", dial_current):
            inert.append(name)
    if not inert:
        return None
    probe = "antineutrino" if antineutrino else "neutrino"
    return (
        f"GENIE dials {', '.join(inert)} cannot act on {probe} jobs with physics.current "
        f"'{current}' (wrong probe or current); their weights are 1 there"
    )


# ── Tables per generator ─────────────────────────────────────────────────────

UNIVERSE_PARAMS: dict[str, tuple[str, ...]] = {
    "nuwro": (*NUWRO_REWEIGHT_PARAMS, *NUWRO_NORM_PARAMS),
    "genie": tuple(GENIE_SCALE_DIALS),
}
VARIATION_PARAMS: dict[str, dict[str, str]] = {"genie": GENIE_SWITCH_DIALS}
BROKEN_PARAMS: dict[str, dict[str, str]] = {
    "nuwro": NUWRO_BROKEN_PARAMS,
    "genie": GENIE_BROKEN_PARAMS,
}

# Physically positive parameters: a non-positive throw is an error, not a value.
POSITIVE_PARAMS = frozenset(
    {"qel_cc_axial_mass", "qel_nc_axial_mass", "qel_s_axial_mass", *NUWRO_NORM_PARAMS,
     *GENIE_SCALE_DIALS}
)


def _check_name(name: str, generator: str, at: str, allowed: Iterable[str]) -> str | None:
    broken = BROKEN_PARAMS.get(generator, {})
    if name in broken:
        return f"{at}: not supported — {broken[name]}"
    allowed = tuple(allowed)
    if name not in allowed:
        return f"{at}: unknown parameter. Supported: {', '.join(allowed)}"
    return None


def validate_universes(block: Any, where: str, generator: str) -> list[str]:
    """Check a ``<generator>.universes`` block, returning error messages."""
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
    switches = VARIATION_PARAMS.get(generator, {})
    for name, spec in parameters.items():
        at = f"{where}.parameters.{name}"
        if name in switches:
            errors.append(
                f"{at}: a switch between two models, not a Gaussian parameter; list it "
                f"under '{generator}.variations' instead"
            )
            continue
        problem = _check_name(name, generator, at, UNIVERSE_PARAMS[generator])
        if problem:
            errors.append(problem)
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
    if generator == "genie":
        names = set(parameters)
        for left, right in GENIE_EXCLUSIVE:
            if names & left and names & right:
                errors.append(
                    f"{where}.parameters: {sorted(names & left)} and {sorted(names & right)} "
                    "select incompatible GENIE Reweight engine modes; use one or the other"
                )
        for group in GENIE_FATE_GROUPS:
            if all(fate in names for fate in group):
                errors.append(
                    f"{where}.parameters: at most three of {', '.join(group)}; GENIE "
                    "Reweight needs the fourth as the cushion term that keeps the fates "
                    "summing to 1"
                )
    return errors


def validate_variations(block: Any, where: str, generator: str) -> list[str]:
    """Check a ``<generator>.variations`` block, returning error messages."""
    switches = VARIATION_PARAMS.get(generator)
    if not switches:
        return [f"{where}: {generator} has no switch-type parameters"]
    if not isinstance(block, dict) or not block:
        return [f"{where}: must be a non-empty mapping of parameter -> settings"]
    errors: list[str] = []
    for name, spec in block.items():
        at = f"{where}.{name}"
        if name in UNIVERSE_PARAMS[generator]:
            errors.append(
                f"{at}: a Gaussian parameter, not a switch; list it under "
                f"'{generator}.universes' instead"
            )
            continue
        problem = _check_name(name, generator, at, switches)
        if problem:
            errors.append(problem)
            continue
        spec = {} if spec is None else spec
        if not isinstance(spec, dict):
            errors.append(f"{at}: must be a mapping (or empty)")
            continue
        unknown = set(spec) - {"value", "source"}
        if unknown:
            errors.append(f"{at}: unknown keys {sorted(unknown)}")
        value = spec.get("value", 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            errors.append(f"{at}.value: must be a number in (0, 1] (got {value!r})")
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


def nuwro_centrals(names: Iterable[str], generation: dict[str, float]) -> dict[str, float]:
    """NuWro central values: the run's own ``e/par`` values, norms at 1.

    ``generation`` must hold every reweighted parameter and ``qel_axial_ff_set``.
    Raises if a parameter has no effect under the run's form-factor choice,
    rather than silently writing weights of 1.
    """
    ff_set = generation.get("qel_axial_ff_set")
    centrals: dict[str, float] = {}
    for name in names:
        required = NUWRO_REWEIGHT_PARAMS.get(name)
        if required is not None and int(ff_set if ff_set is not None else -1) not in required:
            raise ValueError(
                f"Universe parameter {name} has no effect: the run used "
                f"qel_axial_ff_set = {ff_set}, and it only acts under "
                f"{sorted(required)}. Under the MINERvA axial form factor (8) the QE "
                "knob is qel_minerva_ff_scale."
            )
        centrals[name] = 1.0 if name in NUWRO_NORM_PARAMS else float(generation[name])
    return centrals


def norm_factors(
    values: dict[str, np.ndarray], flags: dict[str, np.ndarray], antineutrino: bool
) -> np.ndarray:
    """The ``(n_events, count)`` product of NuWro's channel norm factors.

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


def resolve_universes(block: dict[str, Any], centrals: dict[str, float]) -> dict[str, Any]:
    """Turn a validated block into the universes metadata, given the centrals.

    ``centrals`` holds every parameter's central value, from the generation (see
    ``nuwro_centrals``; GENIE's scale dials are all 1).
    """
    seed, count = int(block["seed"]), int(block["count"])
    parameters: dict[str, dict[str, Any]] = {}
    z: dict[str, list[float]] = {}
    values: dict[str, list[float]] = {}
    for name, spec in block["parameters"].items():
        central = float(centrals[name])
        sigma, log = float(spec["sigma"]), bool(spec.get("log", False))
        throws = draw_z(seed, name, count)
        parameters[name] = {
            "central": central, "sigma": sigma, "log": log, "source": spec.get("source", ""),
        }
        z[name] = throws.tolist()
        values[name] = universe_values(name, central, sigma, log, throws).tolist()
    return {"seed": seed, "count": count, "parameters": parameters, "z": z, "values": values}


def resolve_variations(block: dict[str, Any], generator: str) -> dict[str, Any]:
    """Turn a validated variations block into its metadata; column k is ``columns[k]``."""
    parameters = {
        name: {
            "value": float((spec or {}).get("value", 1)),
            "nominal": 0.0,
            "meaning": VARIATION_PARAMS[generator][name],
            "source": (spec or {}).get("source", ""),
        }
        for name, spec in block.items()
    }
    return {"columns": list(parameters), "parameters": parameters}


def write_spec(path: Path, columns: list[str], rows: Iterable[Iterable[float]]) -> None:
    """The reweighting binary's input: a header of column names, one row per column set."""
    path.write_text(
        " ".join(columns) + "\n" + "".join(" ".join(repr(float(v)) for v in row) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_resolved(work_dir: Path) -> dict[str, Any]:
    """The adapter's ``universes.json``: ``universes``, ``variations`` and
    ``binary_universes`` (whether the binary computed the universe columns)."""
    path = work_dir / RESOLVED_FILE
    if not path.exists():
        raise RuntimeError(
            f"The job defines universes or variations but {path} is missing; the "
            "adapter's reweighting stage did not run."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_weights(
    work_dir: Path,
    resolved: dict[str, Any],
    n_events: int,
    universe_factors: np.ndarray | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """The ``(universe_weights, variation_weights)`` float32 matrices, or None each.

    The binary's ``weights`` tree holds the universe columns first (when
    ``binary_universes``), then one column per variation. ``universe_factors``
    (NuWro's Python-side norms) multiplies the universe columns. Fails on
    non-finite weights, and on columns that never move any weight off 1: a
    parameter with no effect on the sample would otherwise pass for one with no
    uncertainty.
    """
    univ, var = resolved.get("universes"), resolved.get("variations")
    n_univ = int(univ["count"]) if univ and resolved.get("binary_universes") else 0
    n_var = len(var["columns"]) if var else 0
    binary = np.ones((n_events, 0))
    if n_univ + n_var:
        import uproot

        with uproot.open(work_dir / WEIGHTS_FILE) as f:
            binary = np.asarray(f["weights"]["weights"].array(library="np"), dtype=np.float64)
        if binary.shape != (n_events, n_univ + n_var):
            raise RuntimeError(
                f"{WEIGHTS_FILE} has shape {binary.shape}, expected ({n_events}, {n_univ + n_var})"
            )

    universe_weights = None
    if univ:
        universe_weights = binary[:, :n_univ] if n_univ else np.ones((n_events, int(univ["count"])))
        if n_univ and np.all(universe_weights == 1.0):
            raise RuntimeError(
                f"No universe moved any event's weight off 1 for {list(univ['parameters'])}: "
                "none of them acts on this sample (e.g. a strange-axial parameter in a "
                "CC-only run). Refusing to store weights that carry no uncertainty."
            )
        if universe_factors is not None:
            universe_weights = universe_weights * universe_factors
    variation_weights = None
    if var:
        variation_weights = binary[:, n_univ:]
        inert = [name for k, name in enumerate(var["columns"]) if np.all(variation_weights[:, k] == 1.0)]
        if inert:
            raise RuntimeError(
                f"Variations {inert} moved no event's weight off 1: they do not act on "
                "this sample (e.g. RPA_CCQE without CCQE events)."
            )

    for name, matrix in (("universe", universe_weights), ("variation", variation_weights)):
        if matrix is not None and not np.all(np.isfinite(matrix)):
            bad = int((~np.isfinite(matrix)).any(axis=1).sum())
            raise RuntimeError(
                f"{bad} of {n_events} events have a non-finite {name} weight (a zero "
                "nominal cross section, or a NaN from the generator's reweighting)."
            )
    return (
        None if universe_weights is None else universe_weights.astype(np.float32),
        None if variation_weights is None else variation_weights.astype(np.float32),
    )
