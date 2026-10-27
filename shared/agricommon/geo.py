"""Geohash encoding and distance helpers.

Precision-3 geohash cells (~156 km x 156 km) are used as DynamoDB partition
keys for supply and demand. A neighbourhood query is therefore at most nine
key lookups (the cell plus its eight neighbours) and always covers any search
radius up to MAX_RADIUS_KM, after which results are filtered by great-circle
distance. At pilot and 10x volumes a cell holds only tens of listings; if that
grows, raise CELL_PRECISION to 4 and expand rings (see docs/architecture.md).
"""

from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}
EARTH_RADIUS_KM = 6371.0088
CELL_PRECISION = 3
MAX_RADIUS_KM = 150


def encode(lat: float, lon: float, precision: int = 7) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, bit_count, even = [], 0, 0, True
    while len(out) < precision:
        rng, val = (lon_rng, lon) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            bits = (bits << 1) | 1
            rng[0] = mid
        else:
            bits <<= 1
            rng[1] = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_BASE32[bits])
            bits, bit_count = 0, 0
    return "".join(out)


def bounds(gh: str) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max) for a geohash."""
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for ch in gh:
        if ch not in _DECODE:
            raise ValueError(f"invalid geohash character: {ch!r}")
        cd = _DECODE[ch]
        for mask in (16, 8, 4, 2, 1):
            rng = lon_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if cd & mask:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return lat_rng[0], lat_rng[1], lon_rng[0], lon_rng[1]


def decode(gh: str) -> tuple[float, float]:
    la0, la1, lo0, lo1 = bounds(gh)
    return (la0 + la1) / 2, (lo0 + lo1) / 2


def neighbours(gh: str) -> list[str]:
    """The cell itself plus its eight neighbours (deduplicated, stable order)."""
    la0, la1, lo0, lo1 = bounds(gh)
    dlat, dlon = la1 - la0, lo1 - lo0
    clat, clon = (la0 + la1) / 2, (lo0 + lo1) / 2
    cells: list[str] = []
    for dy in (0, 1, -1):
        for dx in (0, 1, -1):
            lat = clat + dy * dlat
            lon = ((clon + dx * dlon + 180) % 360) - 180
            if -90 <= lat <= 90:
                cell = encode(lat, lon, len(gh))
                if cell not in cells:
                    cells.append(cell)
    return cells


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def cell(lat: float, lon: float) -> str:
    return encode(lat, lon, CELL_PRECISION)
