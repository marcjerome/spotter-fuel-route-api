"""OSRM routing client: one HTTP call per new origin/destination pair."""

import hashlib
import logging
from dataclasses import dataclass

import numpy as np
import polyline
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    """The routing service failed or found no route."""


@dataclass(frozen=True)
class Route:
    lat_lng: np.ndarray  # (N, 2) full-resolution geometry
    distance_miles: float
    duration_seconds: float


def _cache_key(start: tuple[float, float], finish: tuple[float, float]) -> str:
    raw = f"{start[0]:.5f},{start[1]:.5f};{finish[0]:.5f},{finish[1]:.5f}"
    return "osrm-route:" + hashlib.sha1(raw.encode()).hexdigest()


# Running totals for GET /api/v1/stats/, kept in the shared cache (Redis in Docker).
STATS_KEYS = {
    "api_calls": "stats:routing:api_calls",
    "cache_hits": "stats:routing:cache_hits",
    "cache_misses": "stats:routing:cache_misses",
}


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> tuple[Route, int]:
    """Return the driving route and how many HTTP calls to OSRM it took (0 = cache hit)."""
    key = _cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        _bump("cache_hits")
        return cached, 0
    _bump("cache_misses")
    route, calls = _fetch_route(start, finish)
    cache.set(key, route)
    return route, calls


def get_stats() -> dict[str, int]:
    return {name: cache.get(key, 0) for name, key in STATS_KEYS.items()}


def reset_stats() -> None:
    cache.delete_many(list(STATS_KEYS.values()))


def _bump(name: str) -> None:
    key = STATS_KEYS[name]
    try:
        cache.incr(key)  # atomic in Redis, so concurrent workers don't lose counts
    except ValueError:  # first use: the key doesn't exist yet
        cache.set(key, 1, timeout=None)


def _fetch_route(start: tuple[float, float], finish: tuple[float, float]) -> tuple[Route, int]:
    """Call the first OSRM server that answers; later servers are failover only."""
    config = settings.ROUTE_PLANNER
    coords = f"{start[1]},{start[0]};{finish[1]},{finish[0]}"  # OSRM expects lng,lat
    params = {"overview": "full", "geometries": "polyline6", "steps": "false", "alternatives": "false"}
    calls = 0
    for base_url in config["OSRM_BASE_URLS"]:
        url = f"{base_url.rstrip('/')}/route/v1/driving/{coords}"
        calls += 1
        _bump("api_calls")
        try:
            response = requests.get(url, params=params, timeout=config["OSRM_TIMEOUT_SECONDS"])
        except requests.RequestException as exc:
            logger.warning("OSRM request to %s failed: %s", base_url, exc)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            logger.warning("OSRM server %s returned HTTP %s", base_url, response.status_code)
            continue
        return _parse_response(response), calls
    raise RoutingError("The routing service is unreachable. Please try again.")


def _parse_response(response: requests.Response) -> Route:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RoutingError(f"The routing service returned HTTP {response.status_code}.") from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        message = payload.get("message") or payload.get("code") or f"HTTP {response.status_code}"
        raise RoutingError(f"No driving route found: {message}")

    best = payload["routes"][0]
    return Route(
        lat_lng=np.array(polyline.decode(best["geometry"], 6), dtype=float),
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=best["duration"],
    )
