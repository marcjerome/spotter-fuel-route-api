"""Resolve user-supplied locations without calling an external geocoder.

Accepted formats:
  "Dallas, TX" / "Dallas, Texas"   looked up in the offline Census place index
  "32.7767,-96.7970"               latitude,longitude
"""

import re
from dataclasses import dataclass

from stations.geocoding import get_place_index

_LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# Rough bounding boxes: contiguous US and Alaska (Hawaii has no road link).
_US_BOUNDS = [
    (24.0, 50.0, -125.5, -66.5),
    (51.0, 71.5, -170.0, -129.9),
]


class LocationError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    query: str
    label: str
    lat: float
    lng: float

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lng)


def in_usa(lat: float, lng: float) -> bool:
    return any(lat_min <= lat <= lat_max and lng_min <= lng <= lng_max
               for lat_min, lat_max, lng_min, lng_max in _US_BOUNDS)


def resolve_location(query: str) -> Location:
    query = query.strip()
    match = _LAT_LNG.match(query)
    if match:
        lat, lng = float(match.group(1)), float(match.group(2))
        if not in_usa(lat, lng):
            raise LocationError(f"'{query}' is outside the USA.")
        return Location(query, f"{lat:.5f}, {lng:.5f}", lat, lng)

    city, sep, state = query.rpartition(",")
    if not sep or not city.strip() or not state.strip():
        raise LocationError(f"'{query}' should look like 'City, ST' or 'latitude,longitude'.")
    place = get_place_index().lookup(city, state)
    if place is None:
        raise LocationError(f"Could not find a US city matching '{query}'.")
    return Location(query, f"{place.name}, {place.state}", place.lat, place.lng)
