"""In-memory spatial index of fuel stations.

Loaded once per process from the database. With ~6.5k stations the whole set
fits in a few hundred KB, and matching stations to a route takes milliseconds,
far faster than a per-request spatial SQL query.
"""

import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import KDTree

from stations.models import FuelStation

from .geo import chord_to_miles, densify, miles_to_chord, to_unit_xyz


@dataclass(frozen=True)
class StationRecord:
    id: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lng: float


@dataclass(frozen=True)
class RouteCandidate:
    """A station near the route, positioned by its distance along the route."""

    station: StationRecord
    mile: float
    detour_miles: float


class StationIndex:
    def __init__(self, stations: list[StationRecord]):
        self.stations = stations
        coords = np.array([(s.lat, s.lng) for s in stations], dtype=float).reshape(-1, 2)
        self._xyz = to_unit_xyz(coords)

    @classmethod
    def from_db(cls) -> "StationIndex":
        rows = FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False).values_list(
            "id", "opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude"
        )
        return cls(
            [
                StationRecord(pk, opis_id, name, address, city, state, float(price), lat, lng)
                for pk, opis_id, name, address, city, state, price, lat, lng in rows
            ]
        )

    def along_route(
        self,
        route_lat_lng: np.ndarray,
        max_detour_miles: float,
        total_miles: float | None = None,
        step_miles: float = 1.0,
    ) -> list[RouteCandidate]:
        """Stations within max_detour_miles of the route, sorted by mile marker.

        If total_miles (the router's road distance) is given, mile markers are
        scaled to it so they agree with the reported route length.
        """
        if not self.stations:
            return []
        points, cumulative = densify(route_lat_lng, step_miles)
        if total_miles and cumulative[-1] > 0:
            cumulative *= total_miles / cumulative[-1]
        route_tree = KDTree(to_unit_xyz(points))
        chord, idx = route_tree.query(self._xyz, distance_upper_bound=miles_to_chord(max_detour_miles))
        near = np.flatnonzero(np.isfinite(chord))
        detours = chord_to_miles(chord[near])
        candidates = [
            RouteCandidate(self.stations[i], float(cumulative[idx[i]]), float(d))
            for i, d in zip(near, detours)
        ]
        candidates.sort(key=lambda c: (c.mile, c.station.price))
        return candidates


_index: StationIndex | None = None
_lock = threading.Lock()


def get_station_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = StationIndex.from_db()
    return _index


def reset_station_index() -> None:
    global _index
    _index = None
