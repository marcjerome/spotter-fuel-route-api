"""Offline geocoding of US city names using the Census Gazetteer places file.

The fuel price CSV has no coordinates, only highway-exit style addresses plus a
city and state. Geocoding ~7k addresses through a free API would be slow and
rate-limited, so each station is placed at its city's centroid instead. The same
index resolves "City, ST" inputs for the route planner without any network call.
"""

import csv
import difflib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

# Trailing lowercase words in Gazetteer names describe the place type
# ("Abbeville city", "Nashville-Davidson metropolitan government (balance)").
_LSAD_SUFFIX = re.compile(r"(\s+(\([a-z ]+\)|[a-z][a-z-]*|CDP|CCD))+$")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_ABBREVIATIONS = {
    "ST": "SAINT",
    "STE": "SAINTE",
    "FT": "FORT",
    "MT": "MOUNT",
    "PT": "PORT",
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {name.upper(): code for code, name in US_STATES.items()}


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lng: float


def normalize_city(name: str) -> str:
    words = _NON_ALNUM.sub(" ", name.upper()).split()
    return " ".join(_ABBREVIATIONS.get(w, w) for w in words)


def normalize_state(state: str) -> str | None:
    state = state.strip().upper()
    if state in US_STATES:
        return state
    return _STATE_BY_NAME.get(state)


class PlaceIndex:
    def __init__(self, path: Path):
        self._places: dict[tuple[str, str], Place] = {}
        self._names_by_state: dict[str, list[str]] = {}
        with open(path, newline="", encoding="utf-8") as f:
            # Incorporated places (funcstat A) win over CDPs sharing the same name.
            rows = sorted(csv.DictReader(f), key=lambda r: r["funcstat"] != "A")
        aliases = []
        for row in rows:
            name = _LSAD_SUFFIX.sub("", row["name"])
            place = Place(name, row["state"], float(row["lat"]), float(row["lng"]))
            self._add(normalize_city(name), place)
            # "Augusta-Richmond County" -> "Augusta", "Boise City" -> "Boise"
            aliases.append((normalize_city(name.split("-")[0]), place))
            aliases.append((normalize_city(re.sub(r" City$", "", name)), place))
        for norm, place in aliases:  # real names take precedence over aliases
            self._add(norm, place)

    def _add(self, norm: str, place: Place) -> None:
        key = (place.state, norm)
        if key not in self._places:
            self._places[key] = place
            self._names_by_state.setdefault(place.state, []).append(norm)

    def lookup(self, city: str, state: str, fuzzy: bool = True) -> Place | None:
        state_code = normalize_state(state)
        if state_code is None:
            return None
        norm = normalize_city(city)
        place = self._places.get((state_code, norm))
        if place or not fuzzy:
            return place
        matches = difflib.get_close_matches(norm, self._names_by_state.get(state_code, []), n=1, cutoff=0.85)
        return self._places[(state_code, matches[0])] if matches else None


@lru_cache(maxsize=1)
def get_place_index() -> PlaceIndex:
    return PlaceIndex(settings.DATA_DIR / "us_places.csv")
