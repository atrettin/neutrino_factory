"""Assertions for event columns read back from a common-output file.

The kinematic and final-state float columns are stored as float32
(``common_output.STORAGE_DTYPES``), so a value read back equals what the
normalizer computed only to float32 precision. Missing values are NaN, which
the plain ``assertEqual``/``assertAlmostEqual`` never consider equal.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# Rounding to float32 changes a value by at most half an ulp, 2**-24 relative.
FLOAT32_RTOL = 2.0**-23


def assert_stored_close(actual: Any, expected: Any, msg: str = "") -> None:
    """``actual`` equals ``expected`` up to float32 storage; NaN matches NaN."""
    np.testing.assert_allclose(actual, expected, rtol=FLOAT32_RTOL, atol=0.0, err_msg=msg)
