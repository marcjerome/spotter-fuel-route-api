"""Route planning pipeline: locations -> route -> nearby stations -> fuel plan."""

import time
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings

from .locations import Location, resolve_location
from .optimizers import STRATEGIES, FuelPlan, FuelProblem, InfeasibleRouteError, naive
from .routing import Route, get_route
from .station_index import RouteCandidate, get_station_index

MAX_GEOMETRY_POINTS = 2000


@dataclass
class PlanRequest:
    start: str
    finish: str
    strategy: str = "greedy"
    start_fuel_gallons: float = 50.0
    max_detour_miles: float = 10.0
    stop_penalty: float = 25.0


@dataclass
class PlanResult:
    request: PlanRequest
    start: Location
    finish: Location
    route: Route
    nearby_stations: list[RouteCandidate]
    candidates: list[RouteCandidate]
    problem: FuelProblem
    plan: FuelPlan
    routing_api_calls: int
    timings_ms: dict[str, float] = field(default_factory=dict)


def prune_dominated(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
    """Keep only the cheapest station at each mile marker.

    Stations are geocoded to city centroids, so every station in a town shares
    one position; a pricier station at the same spot can never be the better
    choice. This shrinks the problem several-fold for the LP/MILP solvers.
    """
    cheapest: dict[float, RouteCandidate] = {}
    for c in candidates:
        key = round(c.mile, 2)
        if key not in cheapest or c.station.price < cheapest[key].station.price:
            cheapest[key] = c
    return sorted(cheapest.values(), key=lambda c: c.mile)


def build_problem(
    candidates: list[RouteCandidate],
    total_miles: float,
    start_fuel_gallons: float,
    start_area_miles: float,
    stop_penalty: float = 0.0,
) -> FuelProblem:
    config = settings.ROUTE_PLANNER
    positions = np.array([c.mile for c in candidates], dtype=float)
    # Stations in the start area count as "fuel up before leaving town", so a
    # vehicle starting empty can use them without driving on zero fuel.
    positions[positions <= start_area_miles] = 0.0
    return FuelProblem(
        positions=positions,
        prices=np.array([c.station.price for c in candidates], dtype=float),
        total_miles=total_miles,
        tank_gallons=config["VEHICLE_RANGE_MILES"] / config["VEHICLE_MPG"],
        mpg=config["VEHICLE_MPG"],
        start_fuel_gallons=start_fuel_gallons,
        stop_penalty=stop_penalty,
    )


def plan_route(request: PlanRequest) -> PlanResult:
    timings: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        timings[name] = round((now - clock) * 1000, 2)
        clock = now

    start = resolve_location(request.start)
    finish = resolve_location(request.finish)
    lap("geocoding")

    route, api_calls = get_route(start.coords, finish.coords)
    lap("routing")

    nearby = get_station_index().along_route(route.lat_lng, request.max_detour_miles, route.distance_miles)
    candidates = prune_dominated(nearby)
    lap("station_matching")

    problem = build_problem(
        candidates,
        route.distance_miles,
        request.start_fuel_gallons,
        request.max_detour_miles,
        request.stop_penalty,
    )
    plan = STRATEGIES[request.strategy](problem)
    lap("optimization")

    return PlanResult(request, start, finish, route, nearby, candidates, problem, plan, api_calls, timings)


def simplify_geometry(lat_lng: np.ndarray, max_points: int = MAX_GEOMETRY_POINTS) -> list[list[float]]:
    """GeoJSON [lng, lat] coordinates, thinned to at most max_points for the response."""
    if len(lat_lng) > max_points:
        keep = np.unique(np.linspace(0, len(lat_lng) - 1, max_points).round().astype(int))
        lat_lng = lat_lng[keep]
    return [[round(lng, 5), round(lat, 5)] for lat, lng in lat_lng.tolist()]


def serialize_result(result: PlanResult, map_url: str | None = None) -> dict:
    problem, plan = result.problem, result.plan
    stops = []
    for seq, i in enumerate(plan.stop_indices, start=1):
        candidate = result.candidates[i]
        station = candidate.station
        gallons = float(plan.purchases[i])
        stops.append(
            {
                "sequence": seq,
                "station": {
                    "id": station.id,
                    "opis_id": station.opis_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "latitude": station.lat,
                    "longitude": station.lng,
                },
                "mile_marker": round(candidate.mile, 1),
                "detour_miles": round(candidate.detour_miles, 1),
                "price_per_gallon": round(station.price, 3),
                "fuel_on_arrival_gallons": round(max(0.0, float(plan.arrival_fuel[i])), 2),
                "gallons_purchased": round(gallons, 2),
                "cost": round(gallons * station.price, 2),
            }
        )

    total_cost = plan.cost(problem)
    gallons = plan.gallons
    return {
        "start": _location(result.start),
        "finish": _location(result.finish),
        "strategy": result.request.strategy,
        "assumptions": {
            "mpg": problem.mpg,
            "vehicle_range_miles": problem.range_miles,
            "tank_capacity_gallons": problem.tank_gallons,
            "start_fuel_gallons": problem.start_fuel_gallons,
            "max_detour_miles": result.request.max_detour_miles,
            "stop_penalty": result.request.stop_penalty if result.request.strategy == "milp" else None,
        },
        "route": {
            "distance_miles": round(result.route.distance_miles, 1),
            "duration_hours": round(result.route.duration_seconds / 3600, 2),
            "geometry": {"type": "LineString", "coordinates": simplify_geometry(result.route.lat_lng)},
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(total_cost, 2),
            "total_gallons_purchased": round(gallons, 2),
            "average_price_per_gallon": round(total_cost / gallons, 3) if gallons else None,
            "number_of_stops": len(stops),
            "stations_near_route": len(result.nearby_stations),
            "stations_considered": len(result.candidates),
            "savings": compute_savings(result),
        },
        "map_url": map_url,
        "meta": {
            "routing_api_calls": result.routing_api_calls,
            "routing_cache_hit": result.routing_api_calls == 0,
            "timings_ms": result.timings_ms,
        },
    }


def compute_savings(result: PlanResult) -> dict:
    """Compare the plan with two baselines, the way fuel apps report "you saved".

    - naive: fill up only when the tank can't reach the next station (no planning).
    - average price: the same gallons bought at the mean price of all stations
      near the route (a typical "vs. average pump price" figure).
    """
    problem, plan = result.problem, result.plan
    cost, gallons = plan.cost(problem), plan.gallons

    try:
        naive_cost = naive(problem).cost(problem)
    except InfeasibleRouteError:
        naive_cost = None

    prices = [c.station.price for c in result.nearby_stations]
    average_price = sum(prices) / len(prices) if prices else None
    average_cost = gallons * average_price if average_price is not None else None

    def compare(baseline: float | None) -> dict | None:
        if baseline is None:
            return None
        saved = baseline - cost
        return {
            "baseline_cost": round(baseline, 2),
            "amount": round(saved, 2),
            "percent": round(saved / baseline * 100, 2) if baseline else 0.0,
        }

    return {
        "vs_naive": compare(naive_cost),
        "vs_average_price": compare(average_cost),
        "average_price_near_route": round(average_price, 3) if average_price is not None else None,
        "per_gallon_vs_average": (
            round(average_price - cost / gallons, 3) if gallons and average_price is not None else None
        ),
    }


def _location(location: Location) -> dict:
    return {"query": location.query, "label": location.label, "latitude": location.lat, "longitude": location.lng}
