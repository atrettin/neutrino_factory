"""The neutrino probes the framework can generate, and their PDG codes.

Single source of truth for every translator: each generator names the probe
differently (GENIE's numeric ``-p``, NuWro's ``beam_particle``, NEUT's
``EVCT-IDPT``, GiBUU's ``flavor_ID`` + signed ``process_ID``), but all of them
derive from this one table, so a flavour is either supported everywhere or
rejected at config validation.
"""

from __future__ import annotations

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
