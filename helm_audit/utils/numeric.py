"""helm_audit.utils.numeric

Small numeric helpers shared across the codebase.

Putting these here avoids the pattern where the same 6-line function is
copy-pasted into diff.py, core_metrics.py, and analysis.py and then drifts
(the analysis.py version had an extra NaN guard the others lacked).
"""

from __future__ import annotations

import math
from typing import Any


def safe_float(x: Any) -> float | None:
    """Cast *x* to float, returning ``None`` on failure or NaN.

    This is the canonical implementation; earlier copies lacked the NaN guard.

    Example:
        >>> safe_float(1)
        1.0
        >>> safe_float('3.14')
        3.14
        >>> safe_float(None) is None
        True
        >>> safe_float(float('nan')) is None
        True
        >>> safe_float('bad') is None
        True
    """
    try:
        if x is None:
            return None
        y = float(x)
        if math.isnan(y):
            return None
        return y
    except Exception:
        return None


def quantile(values: list[float], q: float) -> float | None:
    """Linear-interpolation quantile over an arbitrary (unsorted) sequence.

    The list is sorted internally so callers don't need to pre-sort.

    Parameters
    ----------
    values:
        Sequence of numeric values.  May be unsorted.
    q:
        Quantile in [0, 1].  Values outside this range are clamped.

    Returns
    -------
    float | None
        The interpolated quantile, or ``None`` if *values* is empty.

    Example:
        >>> quantile([3, 1, 2], 0.5)
        2
        >>> quantile([1, 2, 3, 4], 0.25)
        1.75
        >>> quantile([], 0.5) is None
        True
        >>> quantile([5], 0.0)
        5
        >>> quantile([5], 1.0)
        5
    """
    if not values:
        return None
    values = sorted(values)
    if q <= 0:
        return values[0]
    if q >= 1:
        return values[-1]
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    alpha = pos - lo
    return values[lo] * (1 - alpha) + values[hi] * alpha


def nested_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts without chained ``.get()`` calls.

    Replaces patterns like::

        ((((d.get('a') or {}).get('b') or {}).get('c') or {}).get('d'))

    with the readable::

        nested_get(d, 'a', 'b', 'c', 'd')

    Stops and returns *default* as soon as a key is missing or an
    intermediate value is not a dict.

    Example:
        >>> d = {'a': {'b': {'c': 42}}}
        >>> nested_get(d, 'a', 'b', 'c')
        42
        >>> nested_get(d, 'a', 'x', 'c') is None
        True
        >>> nested_get(d, 'a', 'x', 'c', default='missing')
        'missing'
        >>> nested_get(None, 'a') is None
        True
        >>> nested_get({'a': None}, 'a', 'b') is None
        True
    """
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
        if obj is None:
            return default
    return obj if obj is not None else default
