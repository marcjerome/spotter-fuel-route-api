import polyline
import pytest
import requests
from django.core.cache import cache
from django.test import override_settings

from planner import routing

ROUTE_PAYLOAD = {
    "code": "Ok",
    "routes": [
        {
            "geometry": polyline.encode([(32.78, -96.80), (35.0, -94.0), (41.88, -87.63)], 6),
            "distance": 1_555_627.1,
            "duration": 61_570.9,
        }
    ],
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def clear_cache(db):
    cache.clear()


def test_route_is_fetched_once_then_cached(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return FakeResponse(ROUTE_PAYLOAD)

    monkeypatch.setattr(routing.requests, "get", fake_get)
    route, api_calls = routing.get_route((32.78, -96.80), (41.88, -87.63))
    assert api_calls == 1
    assert route.distance_miles == pytest.approx(966.6, abs=0.1)
    assert route.lat_lng.shape == (3, 2)

    _, api_calls = routing.get_route((32.78, -96.80), (41.88, -87.63))
    assert api_calls == 0
    assert len(calls) == 1
    assert routing.get_stats() == {"api_calls": 1, "cache_hits": 1, "cache_misses": 1}
    assert "/route/v1/driving/-96.8,32.78;-87.63,41.88" in calls[0]


@override_settings(ROUTE_PLANNER={"OSRM_BASE_URLS": ["https://a.test", "https://b.test"], "OSRM_TIMEOUT_SECONDS": 1})
def test_fails_over_to_next_server(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        if url.startswith("https://a.test"):
            raise requests.ConnectionError("down")
        return FakeResponse(ROUTE_PAYLOAD)

    monkeypatch.setattr(routing.requests, "get", fake_get)
    _, api_calls = routing.get_route((32.78, -96.80), (41.88, -87.63))
    assert [c.split("/route")[0] for c in calls] == ["https://a.test", "https://b.test"]
    assert api_calls == 2
    assert routing.get_stats()["api_calls"] == 2


def test_no_route_found(monkeypatch):
    monkeypatch.setattr(
        routing.requests, "get", lambda url, params, timeout: FakeResponse({"code": "NoRoute", "message": "No route"})
    )
    with pytest.raises(routing.RoutingError, match="No route"):
        routing.get_route((32.78, -96.80), (41.88, -87.63))


def test_stats_endpoint_and_demo_reset(monkeypatch):
    from django.core.management import call_command
    from django.urls import reverse
    from rest_framework.test import APIClient

    monkeypatch.setattr(routing.requests, "get", lambda url, params, timeout: FakeResponse(ROUTE_PAYLOAD))
    for _ in range(3):
        routing.get_route((32.78, -96.80), (41.88, -87.63))

    stats = APIClient().get(reverse("routing-stats")).json()
    assert stats["route_lookups"] == 3
    assert stats["routing_api_calls"] == 1
    assert (stats["cache_hits"], stats["cache_misses"]) == (2, 1)
    assert stats["cache_hit_rate"] == pytest.approx(0.667)

    call_command("demo_reset")
    stats = APIClient().get(reverse("routing-stats")).json()
    assert stats["route_lookups"] == 0 and stats["cache_hit_rate"] is None
