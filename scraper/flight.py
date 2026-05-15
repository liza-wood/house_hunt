"""Parser for RightMove's window.__PAGE_MODEL.data dedup'd JSON.

Format model:
  - `arr` is a flat array of "slots". Each slot holds either a primitive
    (str/int/float/bool/null) or a structural value (dict/list).
  - Inside a dict or list, every numeric value is a REFERENCE to another slot.
  - When a reference resolves to a primitive slot, that primitive IS the final
    value. We do NOT recurse on it again — that was the v1 bug that turned
    `bedrooms = 2` into the property-id string '171356954' (since arr[2] was
    the property id, and we kept dereferencing).
  - When a reference resolves to a dict/list, we still need to resolve the
    references inside that structure.

This matches the actual observed behaviour on rightmove.co.uk pages as of
May 2026: literal ints (bedrooms count, lease years) live in their own slots,
referenced by integer indices from the structural slots.
"""
from __future__ import annotations

from typing import Any


def resolve_compact_dedup(arr: list) -> Any:
    if not isinstance(arr, list) or not arr:
        return arr

    memo: dict[int, Any] = {}
    in_progress: set[int] = set()

    def resolve_idx(i: int) -> Any:
        """Return the value at slot i, with any nested references resolved."""
        if i in memo:
            return memo[i]
        if i in in_progress:
            return None  # cycle guard
        if not (0 <= i < len(arr)):
            return i  # out-of-range; treat as a literal int
        in_progress.add(i)
        try:
            target = arr[i]
            if isinstance(target, dict):
                result = {k: resolve_field(v) for k, v in target.items()}
            elif isinstance(target, list):
                result = [resolve_field(v) for v in target]
            else:
                # PRIMITIVE slot — the literal value. Crucially: do not recurse.
                result = target
            memo[i] = result
            return result
        finally:
            in_progress.discard(i)

    def resolve_field(v: Any) -> Any:
        """Resolve a value found INSIDE a structural slot: ints are references."""
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, int):
            return resolve_idx(v)
        if isinstance(v, (str, float)):
            return v
        # Nested dicts/lists inside a structural slot (defensive — not seen in practice)
        if isinstance(v, dict):
            return {k: resolve_field(val) for k, val in v.items()}
        if isinstance(v, list):
            return [resolve_field(x) for x in v]
        return v

    return resolve_idx(0)
