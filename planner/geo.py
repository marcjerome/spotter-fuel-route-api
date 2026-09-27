"""Small vectorised geometry helpers (numpy only, no GIS dependencies)."""

import numpy as np

EARTH_RADIUS_MILES = 3958.8


def to_unit_xyz(lat_lng: np.ndarray) -> np.ndarray:
    """Convert an (N, 2) array of lat/lng degrees to (N, 3) points on the unit sphere.

    Euclidean distance between these points is the chord length, which lets a
    KD-tree answer "within X miles" queries on a sphere.
    """
    lat = np.radians(lat_lng[:, 0])
    lng = np.radians(lat_lng[:, 1])
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lng), cos_lat * np.sin(lng), np.sin(lat)))


def miles_to_chord(miles: float) -> float:
    return 2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord: np.ndarray) -> np.ndarray:
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(chord / 2.0, 0.0, 1.0))


def haversine_miles(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Great-circle distance between matching rows of two (N, 2) lat/lng arrays."""
    lat1, lng1, lat2, lng2 = map(np.radians, (a[:, 0], a[:, 1], b[:, 0], b[:, 1]))
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(h))


def densify(lat_lng: np.ndarray, max_step_miles: float) -> tuple[np.ndarray, np.ndarray]:
    """Insert points so no segment is longer than max_step_miles.

    Returns the densified (M, 2) points and the cumulative distance in miles at
    each point. Long straight interstate segments can otherwise have vertices
    tens of miles apart, which would hide stations near their midpoint.
    """
    seg = haversine_miles(lat_lng[:-1], lat_lng[1:])
    pieces = np.maximum(1, np.ceil(seg / max_step_miles).astype(int))
    starts = np.repeat(lat_lng[:-1], pieces, axis=0)
    ends = np.repeat(lat_lng[1:], pieces, axis=0)
    # Fraction along each segment for every inserted point: 0, 1/k, ..., (k-1)/k
    offsets = np.arange(pieces.sum()) - np.repeat(np.cumsum(pieces) - pieces, pieces)
    t = (offsets / np.repeat(pieces, pieces))[:, None]
    points = np.vstack((starts + (ends - starts) * t, lat_lng[-1:]))
    step = np.repeat(seg / pieces, pieces)
    cumulative = np.concatenate(([0.0], np.cumsum(step)))
    return points, cumulative
