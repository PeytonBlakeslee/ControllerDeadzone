"""Pure response math and event interpretation. No native field access."""
from __future__ import annotations
import math
from types import MappingProxyType

# These are the unchanged native backend notification kinds, not SDK enum values.
NATIVE_EVENT_KINDS = MappingProxyType({'pressed': 0, 'released': 1, 'repeat': 2})


def event_mapping(enum):
    result = {}
    for name, kind in (('IE_Pressed', 'pressed'), ('IE_Released', 'released'),
                       ('IE_Repeat', 'repeat'), ('IE_DoubleClick', 'pressed')):
        value = getattr(enum, name, None)
        if value is not None:
            result[int(value)] = kind
    return MappingProxyType(result)


def key_event_kind(event, mapping):
    try:
        number = int(event)
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is not None:
        return mapping.get(number, 'unknown')
    name = str(getattr(event, 'name', event)).lower()
    if 'released' in name or 'release' in name:
        return 'released'
    if 'repeat' in name:
        return 'repeat'
    if 'pressed' in name or 'press' in name or 'doubleclick' in name or 'double_click' in name:
        return 'pressed'
    return 'unknown'


def radial_multiplier(x: float, y: float, inner: float, outer: float) -> float:
    """Circular, direction-preserving inner/outer response for a normalized pair.

    r is clipped to the unit disc at the outer endpoint, but the original
    direction is preserved. A 100% inner threshold is an unconditional block.
    """
    if inner >= 1.0:
        return 0.0
    radius = math.hypot(x, y)
    if radius <= inner or radius <= 0.0:
        return 0.0
    # Illegal persisted inner >= outer values use a hard outer transition
    # rather than divide by zero or produce an invalid/negative multiplier.
    if outer <= inner:
        mapped = 1.0
    else:
        mapped = min(1.0, (radius - inner) / (outer - inner))
    return mapped / radius
