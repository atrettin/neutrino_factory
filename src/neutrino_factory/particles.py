"""The neutrino probes and nuclear targets the framework can generate, and their
PDG codes.

Single source of truth for every translator: each generator names the probe
differently (GENIE's numeric ``-p``, NuWro's ``beam_particle``, NEUT's
``EVCT-IDPT``, GiBUU's ``flavor_ID`` + signed ``process_ID``), but all of them
derive from this one table, so a flavour is either supported everywhere or
rejected at config validation. The same holds for the target nucleus: its PDG
code and its (Z, N) composition are *derived* from the name, so a config-time
check and a run-time lookup can never disagree.
"""

from __future__ import annotations

import re

# PDG codes of the six neutrino probes. Antineutrinos are the negated codes,
# which is what carries the "is this an antineutrino" information downstream —
# never the "bar" suffix of the name.
PARTICLE_PDG: dict[str, int] = {
    "nue": 12,
    "nuebar": -12,
    "numu": 14,
    "numubar": -14,
    "nutau": 16,
    "nutaubar": -16,
}


def probe_pdg(particle: str, context: str) -> int:
    """PDG code of ``particle``, or a KeyError naming the accepted flavours.

    ``context`` is the generator (or component) the lookup is for, so the error
    says where the unusable name was found.
    """
    try:
        return PARTICLE_PDG[particle]
    except KeyError:
        raise KeyError(
            f"Unknown neutrino particle '{particle}' for {context} probe. "
            f"Known: {', '.join(PARTICLE_PDG)}"
        ) from None


def is_antineutrino(particle: str, context: str = "framework") -> bool:
    """True for the antineutrino probes, decided by the sign of the PDG code."""
    return probe_pdg(particle, context) < 0


# Element symbols indexed by atomic number, ELEMENT_SYMBOLS[Z - 1]. Nuclei are
# named, not enumerated: keeping one table of elements rather than a table of
# nuclides means any isotope a generator supports is usable without a code
# change, and the config-time check is exactly the run-time lookup.
ELEMENT_SYMBOLS: tuple[str, ...] = (
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca",
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr",
    "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th",
    "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm",
    "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds",
    "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
)

ELEMENT_Z: dict[str, int] = {
    symbol: number for number, symbol in enumerate(ELEMENT_SYMBOLS, start=1)
}

# An element symbol followed by the mass number: "C12", "Ar40", "Fe56".
_NUCLEUS_PATTERN = re.compile(r"^([A-Z][a-z]?)(\d+)$")


def nucleus_z_a(nucleus: str) -> tuple[int, int]:
    """``(Z, A)`` for a nucleus named like ``C12``, ``Ar40``, ``Fe56``.

    Raises ``ValueError`` — never a silent fallback — for anything unparsable,
    unknown, or physically impossible: an invented nucleus that survived into a
    generator run would produce events on the wrong target without saying so.
    """
    match = _NUCLEUS_PATTERN.match(str(nucleus).strip())
    if match is None:
        raise ValueError(
            f"Cannot parse nucleus name '{nucleus}'. Expected an element symbol "
            "followed by the mass number, e.g. 'C12', 'Ar40', 'Fe56'."
        )
    symbol, mass_number = match.group(1), int(match.group(2))
    if symbol not in ELEMENT_Z:
        raise ValueError(f"Unknown element symbol '{symbol}' in nucleus '{nucleus}'.")
    atomic_number = ELEMENT_Z[symbol]
    if mass_number < atomic_number:
        raise ValueError(
            f"Nucleus '{nucleus}' has mass number {mass_number} below its proton "
            f"number {atomic_number}."
        )
    return atomic_number, mass_number


def nucleus_pdg(nucleus: str) -> int:
    """Nuclear PDG code of ``nucleus`` in the standard ``10LZZZAAAI`` form.

    ``L`` (strangeness) and ``I`` (isomer level) are zero: the framework only
    generates on ground-state, non-strange nuclei. ``C12`` -> ``1000060120``.
    """
    atomic_number, mass_number = nucleus_z_a(nucleus)
    return 1_000_000_000 + atomic_number * 10_000 + mass_number * 10


def nucleus_composition(nucleus: str) -> tuple[int, int]:
    """``(protons, neutrons)`` of ``nucleus``, for generators that want them
    separately (NuWro's ``nucleus_p``/``nucleus_n``, NEUT, GiBUU)."""
    atomic_number, mass_number = nucleus_z_a(nucleus)
    return atomic_number, mass_number - atomic_number
