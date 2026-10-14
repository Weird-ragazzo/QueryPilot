"""Deterministic result comparison logic for text-to-SQL evaluation."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ComparisonResult:
    matched: bool
    reason: str | None = None
    expected_rows: int = 0
    actual_rows: int = 0
    order_matters: bool = False


def _normalize_value(val: Any, float_tolerance: float = 1e-2) -> Any:
    """Normalize values for robust equality comparison."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # Convert near-integer floats to rounded representation
        f = float(val)
        if math.isnan(f):
            return "NaN"
        return round(f, 4)
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, str):
        return val.strip()
    return val


def _values_equal(a: Any, b: Any, float_tolerance: float = 1e-2) -> bool:
    """Check if two cell values are equivalent with numeric tolerance."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-3, abs_tol=float_tolerance)
    if isinstance(a, (int, float)) and isinstance(b, str):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-3, abs_tol=float_tolerance)
        except ValueError:
            return False
    if isinstance(b, (int, float)) and isinstance(a, str):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-3, abs_tol=float_tolerance)
        except ValueError:
            return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return a == b


def _row_dict_to_canonical(row: dict[str, Any], float_tolerance: float = 1e-2) -> dict[str, Any]:
    """Lowercase keys and normalize values for flexible matching."""
    return {str(k).strip().lower(): _normalize_value(v, float_tolerance) for k, v in row.items()}


def _rows_match(row_a: dict[str, Any], row_b: dict[str, Any], float_tolerance: float = 1e-2) -> bool:
    """Check if two row dictionaries have matching columns and values."""
    canon_a = {str(k).strip().lower(): v for k, v in row_a.items()}
    canon_b = {str(k).strip().lower(): v for k, v in row_b.items()}

    # If column keys match exactly:
    if set(canon_a.keys()) == set(canon_b.keys()):
        return all(_values_equal(canon_a[k], canon_b[k], float_tolerance) for k in canon_a)

    # If columns have different alias names (e.g. SUM(...) vs revenue) but same values in positional order:
    if len(canon_a) == len(canon_b):
        vals_a = list(canon_a.values())
        vals_b = list(canon_b.values())
        if all(_values_equal(va, vb, float_tolerance) for va, vb in zip(vals_a, vals_b)):
            return True

    return False


def compare_results(
    actual: list[dict[str, Any]] | None,
    expected: list[dict[str, Any]] | None,
    order_matters: bool = False,
    float_tolerance: float = 1e-2,
) -> ComparisonResult:
    """Compare query execution results against golden answers.

    Handles:
    - Empty result sets
    - Row order only when explicitly requested
    - Column naming variations / case insensitivity
    - Floating point tolerances
    - NULL values
    """
    actual_rows = list(actual or [])
    expected_rows = list(expected or [])

    actual_len = len(actual_rows)
    expected_len = len(expected_rows)

    if actual_len != expected_len:
        return ComparisonResult(
            matched=False,
            reason=f"Row count mismatch: expected {expected_len}, got {actual_len}",
            expected_rows=expected_len,
            actual_rows=actual_len,
            order_matters=order_matters,
        )

    if expected_len == 0:
        return ComparisonResult(
            matched=True,
            expected_rows=0,
            actual_rows=0,
            order_matters=order_matters,
        )

    if order_matters:
        for idx, (exp_row, act_row) in enumerate(zip(expected_rows, actual_rows)):
            if not _rows_match(exp_row, act_row, float_tolerance):
                return ComparisonResult(
                    matched=False,
                    reason=f"Row {idx} mismatch: expected {exp_row}, got {act_row}",
                    expected_rows=expected_len,
                    actual_rows=actual_len,
                    order_matters=True,
                )
        return ComparisonResult(
            matched=True,
            expected_rows=expected_len,
            actual_rows=actual_len,
            order_matters=True,
        )

    # Order does not matter: multiset comparison
    unmatched_actual = list(actual_rows)
    for exp_row in expected_rows:
        matched_idx = None
        for idx, act_row in enumerate(unmatched_actual):
            if _rows_match(exp_row, act_row, float_tolerance):
                matched_idx = idx
                break
        if matched_idx is None:
            return ComparisonResult(
                matched=False,
                reason=f"Expected row not found in actual results: {exp_row}",
                expected_rows=expected_len,
                actual_rows=actual_len,
                order_matters=False,
            )
        unmatched_actual.pop(matched_idx)

    return ComparisonResult(
        matched=True,
        expected_rows=expected_len,
        actual_rows=actual_len,
        order_matters=False,
    )
