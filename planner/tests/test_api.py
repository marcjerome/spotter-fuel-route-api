from decimal import Decimal

import numpy as np
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from planner import services
from planner.routing import Route, RoutingError
from planner.station_index import reset_station_index
from stations.models import FuelStation

# A straight route from Oklahoma City, OK east to Nashville, TN (~680 miles).
START = (35.4676, -97.5164)
FINISH = (36.1627, -86.7816)


def straight_route(start, finish, n=400) -> Route:
    lat_lng = np.column_stack((np.linspace(start[0], finish[0], n), np.linspace(start[1], finish[1], n)))
    return Route(lat_lng=lat_lng, distance_miles=680.0, duration_seconds=10 * 3600)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def routing_calls(monkeypatch):
    calls = []

    def fake_get_route(start, finish):
        calls.append((start, finish))
        return straight_route(start, finish), 1

    monkeypatch.setattr(services, "get_route", fake_get_route)
    return calls


@pytest.fixture
def stations(db):
    reset_station_index()
    rows = [
        # (opis_id, name, city, state, price, lat, lng)
        (1, "Start Stop", "Oklahoma City", "OK", "3.50", 35.47, -97.52),
        (2, "Cheap Middle", "Fort Smith", "AR", "2.90", 35.80, -92.10),
        (3, "Pricey Middle", "Fort Smith", "AR", "3.90", 35.80, -92.10),
        (4, "Late Stop", "Jackson", "TN", "3.20", 36.00, -89.50),
        (5, "Far Away", "Denver", "CO", "1.00", 39.74, -104.99),
    ]
    FuelStation.objects.bulk_create(
        FuelStation(
            opis_id=o, name=n, address="I-40", city=c, state=s, retail_price=Decimal(p), latitude=la, longitude=lo
        )
        for o, n, c, s, p, la, lo in rows
    )
    yield
    reset_station_index()


@pytest.mark.django_db
class TestRoutePlanAPI:
    url = reverse("route-plan")

    def test_plans_cheapest_stops(self, api, stations, routing_calls):
        response = api.post(
            self.url,
            {"start": "Oklahoma City, OK", "finish": "Nashville, TN", "start_fuel_gallons": 0},
            format="json",
        )
        assert response.status_code == 200, response.content
        data = response.json()

        assert len(routing_calls) == 1
        assert data["strategy"] == "greedy"
        assert data["route"]["distance_miles"] == 680.0
        assert data["route"]["geometry"]["type"] == "LineString"

        names = [stop["station"]["name"] for stop in data["fuel_stops"]]
        assert "Far Away" not in names  # off the route
        assert "Pricey Middle" not in names  # dominated by the cheaper station in the same town
        assert "Cheap Middle" in names

        summary = data["summary"]
        assert summary["total_gallons_purchased"] == pytest.approx(68.0, abs=0.01)
        assert summary["total_fuel_cost"] == pytest.approx(
            sum(stop["cost"] for stop in data["fuel_stops"]), abs=0.05
        )
        assert data["map_url"].startswith("http://testserver/api/v1/route-plan/map/?")

        savings = summary["savings"]
        assert savings["vs_naive"]["baseline_cost"] >= summary["total_fuel_cost"]
        assert savings["vs_naive"]["amount"] == pytest.approx(
            savings["vs_naive"]["baseline_cost"] - summary["total_fuel_cost"], abs=0.01
        )
        # Average of the 4 on-route stations: (3.50 + 2.90 + 3.90 + 3.20) / 4
        assert savings["average_price_near_route"] == pytest.approx(3.375)

    def test_coordinates_as_input(self, api, stations, routing_calls):
        response = api.post(self.url, {"start": f"{START[0]},{START[1]}", "finish": "Nashville, TN"}, format="json")
        assert response.status_code == 200, response.content
        assert response.json()["start"]["latitude"] == START[0]

    def test_get_is_not_allowed(self, api, stations, routing_calls):
        response = api.get(self.url, {"start": "Oklahoma City, OK", "finish": "Nashville, TN"})
        assert response.status_code == 405
        assert routing_calls == []

    @pytest.mark.parametrize("strategy", ["greedy", "lp", "milp", "naive"])
    def test_every_strategy(self, api, stations, routing_calls, strategy):
        response = api.post(
            self.url,
            {"start": "Oklahoma City, OK", "finish": "Nashville, TN", "strategy": strategy},
            format="json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["strategy"] == strategy

    def test_starts_with_full_tank_by_default(self, api, stations, routing_calls):
        response = api.post(self.url, {"start": "Oklahoma City, OK", "finish": "Nashville, TN"}, format="json")
        data = response.json()
        assert data["assumptions"]["start_fuel_gallons"] == 50
        # 68 gallons needed, 50 already on board.
        assert data["summary"]["total_gallons_purchased"] == pytest.approx(18.0, abs=0.01)

    def test_unknown_city(self, api, stations, routing_calls):
        response = api.post(self.url, {"start": "Atlantis, ZZ", "finish": "Nashville, TN"}, format="json")
        assert response.status_code == 400
        assert routing_calls == []

    def test_outside_usa(self, api, stations, routing_calls):
        response = api.post(self.url, {"start": "48.8566,2.3522", "finish": "Nashville, TN"}, format="json")
        assert response.status_code == 400
        assert "outside the USA" in response.json()["detail"]

    def test_validation_errors(self, api, stations, routing_calls):
        response = api.post(self.url, {"start": "Oklahoma City, OK", "start_fuel_gallons": 80}, format="json")
        assert response.status_code == 400
        assert set(response.json()) == {"finish", "start_fuel_gallons"}

    def test_infeasible_route(self, api, db, routing_calls):
        reset_station_index()  # no stations at all
        response = api.post(self.url, {"start": "Oklahoma City, OK", "finish": "Nashville, TN"}, format="json")
        assert response.status_code == 422

    def test_routing_failure(self, api, stations, monkeypatch):
        def broken(start, finish):
            raise RoutingError("The routing service is unreachable. Please try again.")

        monkeypatch.setattr(services, "get_route", broken)
        response = api.post(self.url, {"start": "Oklahoma City, OK", "finish": "Nashville, TN"}, format="json")
        assert response.status_code == 502


@pytest.mark.django_db
def test_map_page_renders(api, stations, routing_calls):
    response = api.get(reverse("route-plan-map"), {"start": "Oklahoma City, OK", "finish": "Nashville, TN"})
    assert response.status_code == 200
    body = response.content.decode()
    assert "leaflet" in body
    assert 'id="plan-data"' in body


@pytest.mark.django_db
def test_schema_and_docs(api):
    assert api.get(reverse("schema")).status_code == 200
    assert api.get(reverse("swagger-ui")).status_code == 200
