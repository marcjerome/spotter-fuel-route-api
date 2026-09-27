import numpy as np
import pytest
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from planner.locations import LocationError, resolve_location
from planner.station_index import StationIndex, StationRecord
from stations.geocoding import get_place_index, normalize_city
from stations.models import FuelStation

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.799
30,NOWHERE STOP,"I-1, EXIT 1",Qwxyzville,TX,1,3.10
40,CANADIAN STOP,"HWY 1",Calgary,AB,1,1.50
"""


class TestGeocoding:
    @pytest.mark.parametrize(
        "city,state",
        [
            ("Dallas", "TX"),
            ("St. Louis", "MO"),
            ("Saint Louis", "Missouri"),
            ("Nashville", "TN"),  # "Nashville-Davidson metropolitan government (balance)"
            ("Boise", "ID"),  # "Boise City city"
            ("Mahwah", "NJ"),  # township, from the county subdivision file
        ],
    )
    def test_lookup(self, city, state):
        assert get_place_index().lookup(city, state) is not None

    def test_unknown_city(self):
        assert get_place_index().lookup("Qwxyzville", "TX") is None

    def test_normalize(self):
        assert normalize_city("Ft. Worth") == "FORT WORTH"


class TestResolveLocation:
    def test_city_state(self):
        loc = resolve_location("chicago, il")
        assert loc.label == "Chicago, IL"
        assert 41 < loc.lat < 42.5

    def test_coordinates(self):
        assert resolve_location(" 40.7128 , -74.0060 ").coords == (40.7128, -74.006)

    @pytest.mark.parametrize("query", ["Chicago", "51.5,-0.12", "Paris, FR"])
    def test_rejects(self, query):
        with pytest.raises(LocationError):
            resolve_location(query)


def test_station_index_along_route():
    stations = [
        StationRecord(1, 1, "on route", "", "", "TX", 3.0, 32.0, -99.0),
        StationRecord(2, 2, "8 mi off", "", "", "TX", 3.0, 32.116, -98.0),
        StationRecord(3, 3, "far", "", "", "TX", 3.0, 33.0, -98.0),
    ]
    route = np.array([[32.0, -100.0], [32.0, -97.0]])
    found = StationIndex(stations).along_route(route, max_detour_miles=10)
    assert [c.station.name for c in found] == ["on route", "8 mi off"]
    assert found[0].mile == pytest.approx(58.7, abs=1.5)  # 1 degree of longitude at 32N
    assert found[1].detour_miles == pytest.approx(8.0, abs=0.3)


@pytest.mark.django_db
class TestLoadStations:
    def test_import(self, tmp_path):
        path = tmp_path / "prices.csv"
        path.write_text(CSV)
        call_command("load_stations", csv=str(path))

        assert FuelStation.objects.count() == 3  # Canadian row skipped, duplicate id merged
        gila = FuelStation.objects.get(opis_id=20)
        assert str(gila.retail_price) == "3.79900"  # cheapest duplicate kept
        assert gila.has_coordinates
        assert not FuelStation.objects.get(opis_id=30).has_coordinates

    def test_reimport_is_idempotent(self, tmp_path):
        path = tmp_path / "prices.csv"
        path.write_text(CSV)
        call_command("load_stations", csv=str(path))
        call_command("load_stations", csv=str(path))
        assert FuelStation.objects.count() == 3


@pytest.mark.django_db
def test_station_list_filters():
    FuelStation.objects.create(opis_id=1, name="A", address="", city="Austin", state="TX", retail_price="3.1")
    FuelStation.objects.create(opis_id=2, name="B", address="", city="Waco", state="TX", retail_price="2.9")
    FuelStation.objects.create(opis_id=3, name="C", address="", city="Tulsa", state="OK", retail_price="2.5")

    response = APIClient().get(reverse("station-list"), {"state": "tx", "ordering": "price"})
    assert [s["name"] for s in response.json()["results"]] == ["B", "A"]

    response = APIClient().get(reverse("station-list"), {"max_price": "3.0"})
    assert {s["name"] for s in response.json()["results"]} == {"B", "C"}
