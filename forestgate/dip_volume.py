"""Tank-body coordinate integration for tilted emergency fuel tanks.

The depot tanks are rectangular shells resting on temporary legs. A patrol can
only read a single dipstick at the tank head (x = 0); the leg grade is not
observable from that one reading, so one dipstick value is consistent with
more than one physical stock. This module turns geometry + dip + attitude
evidence into litre-graded volume bounds. It is deliberately pure (no I/O, no
sqlite) so the physical counter-examples are testable to the litre.

Coordinate frame
----------------
* ``x`` runs along the tank length from the dip at the head, ``0 <= x <= L``.
* The free liquid surface is horizontal. A head dipstick reads ``h`` metres:
  the vertical distance from the head bottom to that surface.
* ``s`` is the bottom grade in metres/metre: ``s > 0`` means the bottom falls
  away from the head, so before wall clamps the liquid depth at ``x`` is
  ``d(x) = h + s*x``. With ``L=2, h=0.3``: ``s=+0.1`` gives far-end depth 0.5
  and 800 L, ``s=-0.1`` gives far-end depth 0.1 and 400 L, while a naive
  level-tank conversion reports a single false 600 L for both worlds.
* Depths are clamped to ``[0, H]`` to handle a dry wedge or an overflowing
  crown analytically (piecewise trapezoid integration, no numeric solver).
* The suction intake sits ``dead_m`` above the head bottom. Oil below the
  horizontal intake plane cannot reach the pump and is dead stock: the
  extractable profile is ``clamp(h + s*x, 0, h - dead_m)``.

Measurement uncertainty
-----------------------
Every volume is monotone non-decreasing in both ``h`` and ``s`` on
``x >= 0``, so certified lower/upper bounds are obtained by evaluating the
same integration at the error endpoints. Without an attitude record the slope
may be anywhere inside the approved leg-grade band, which yields an evidenced
*range* rather than a single stock; picking the most favourable endpoint as
"the" stock is explicitly unsupported.
"""
from dataclasses import dataclass
from math import ceil, floor

LITRES_PER_M3 = 1000.0


@dataclass(frozen=True)
class TankGeometry:
    """Rectangular tank shell and suction-pipe dead level."""

    length_m: float
    width_m: float
    height_m: float
    dead_m: float = 0.0
    slope_min: float = -0.10
    slope_max: float = 0.10

    def __post_init__(self):
        if min(self.length_m, self.width_m, self.height_m) <= 0:
            raise ValueError('geometry_dimensions_invalid')
        if not 0 <= self.dead_m <= self.height_m:
            raise ValueError('dead_level_invalid')
        if self.slope_min > self.slope_max:
            raise ValueError('slope_band_invalid')


def _clamped_linear_integral(length_m, intercept, slope, lower, upper):
    """Integral over [0, L] of clamp(intercept + slope*x, lower, upper)."""
    if upper <= lower:
        return 0.0
    breaks = [0.0, float(length_m)]
    if slope != 0.0:
        for level in (lower, upper):
            x = (level - intercept) / slope
            if 0.0 < x < length_m:
                breaks.append(x)
    breaks = sorted(set(breaks))
    area = 0.0
    for x0, x1 in zip(breaks, breaks[1:]):
        width = x1 - x0
        midpoint = intercept + slope * (x0 + x1) / 2.0
        if midpoint <= lower:
            area += lower * width
        elif midpoint >= upper:
            area += upper * width
        else:
            d0 = intercept + slope * x0
            d1 = intercept + slope * x1
            area += (d0 + d1) / 2.0 * width
    return area


def gross_volume_l(geometry, head_depth_m, slope):
    """Total liquid litres present, including dead stock."""
    area = _clamped_linear_integral(
        geometry.length_m, head_depth_m, slope, 0.0, geometry.height_m)
    # Round to micro-litre precision so exact analytic wedges (e.g. exactly
    # 400 L) are not perturbed by float representation noise.
    return round(geometry.width_m * area * LITRES_PER_M3, 6)


def extractable_volume_l(geometry, head_depth_m, slope):
    """Pumpable litres: gross volume minus the intake-plane dead wedge."""
    ceiling = head_depth_m - geometry.dead_m
    if ceiling <= 0:
        return 0.0
    area = _clamped_linear_integral(
        geometry.length_m, head_depth_m, slope, 0.0,
        min(ceiling, geometry.height_m))
    return round(geometry.width_m * area * LITRES_PER_M3, 6)


def _bounds(volume_fn, geometry, head_depth_m, depth_error_m, slope, slope_error_m):
    low = volume_fn(geometry, head_depth_m - depth_error_m, slope - slope_error_m)
    high = volume_fn(geometry, head_depth_m + depth_error_m, slope + slope_error_m)
    return low, high


def gross_range_l(geometry, head_depth_m, depth_error_m, slope, slope_error_m):
    """Certified (low, high) gross litres including both measurement errors."""
    return _bounds(gross_volume_l, geometry, head_depth_m, depth_error_m,
                   slope, slope_error_m)


def extractable_range_l(geometry, head_depth_m, depth_error_m, slope, slope_error_m):
    """Certified (low, high) pumpable litres including dead stock and errors."""
    return _bounds(extractable_volume_l, geometry, head_depth_m, depth_error_m,
                   slope, slope_error_m)


def dip_only_ranges_l(geometry, head_depth_m, depth_error_m):
    """Evidenced ranges when no attitude record exists for this dip.

    The slope is only constrained by the approved leg-grade band, so this is
    an interval. It must never be collapsed to the most favourable endpoint.
    """
    gross_low = gross_volume_l(geometry, head_depth_m - depth_error_m, geometry.slope_min)
    gross_high = gross_volume_l(geometry, head_depth_m + depth_error_m, geometry.slope_max)
    extractable_low = extractable_volume_l(
        geometry, head_depth_m - depth_error_m, geometry.slope_min)
    extractable_high = extractable_volume_l(
        geometry, head_depth_m + depth_error_m, geometry.slope_max)
    return {
        'gross_low_l': gross_low,
        'gross_high_l': gross_high,
        'extractable_low_l': extractable_low,
        'extractable_high_l': extractable_high,
    }


def whole_litre_bounds(low_l, high_l):
    """Conservative integer-litre guarantees from float bounds.

    Inputs are already rounded to micro-litres, so floor/ceil here never
    collide with representation noise and remain strict guarantees.
    """
    return floor(low_l), ceil(high_l)


def naive_level_volume_l(geometry, head_depth_m):
    """The conventional single-value conversion: pretend the tank is level."""
    depth = max(0.0, min(head_depth_m, geometry.height_m))
    return geometry.length_m * geometry.width_m * depth * LITRES_PER_M3
