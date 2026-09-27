from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import RoutePlanJob
from .optimizers import STRATEGIES

_config = settings.ROUTE_PLANNER
TANK_GALLONS = _config["VEHICLE_RANGE_MILES"] / _config["VEHICLE_MPG"]


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=200, help_text="Start location: 'City, ST' (e.g. 'Dallas, TX') or 'lat,lng'."
    )
    finish = serializers.CharField(
        max_length=200, help_text="Finish location: 'City, ST' (e.g. 'Chicago, IL') or 'lat,lng'."
    )
    strategy = serializers.ChoiceField(
        choices=sorted(STRATEGIES),
        default=_config["DEFAULT_STRATEGY"],
        help_text=(
            "greedy: cheapest plan, fast (default). lp: same optimum via linear programming. "
            "milp: trades fuel cost against number of stops using stop_penalty. "
            "naive: fill up only when the tank can't reach the next station (baseline)."
        ),
    )
    start_fuel_gallons = serializers.FloatField(
        default=TANK_GALLONS,
        min_value=0.0,
        max_value=TANK_GALLONS,
        help_text=(
            f"Fuel in the tank at the start (0-{TANK_GALLONS:g} gallons, default full). With 0 the "
            "vehicle fuels up at a station near the start before leaving."
        ),
    )
    max_detour_miles = serializers.FloatField(
        default=_config["DEFAULT_MAX_DETOUR_MILES"],
        min_value=1.0,
        max_value=50.0,
        help_text="How far off the route a station may be to be considered.",
    )
    stop_penalty = serializers.FloatField(
        default=25.0,
        min_value=0.0,
        max_value=1000.0,
        help_text="Dollar cost assigned to each fuel stop (milp strategy only).",
    )
    mode = serializers.ChoiceField(
        choices=["sync", "async"],
        default="sync",
        help_text=(
            "sync: compute now and return the plan (default). async: return 202 with a job "
            "computed by a background worker; poll its status_url."
        ),
    )


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    label = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class StationSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    opis_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class FuelStopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    station = StationSummarySerializer()
    mile_marker = serializers.FloatField(help_text="Distance along the route in miles.")
    detour_miles = serializers.FloatField(help_text="Straight-line distance from the route.")
    price_per_gallon = serializers.FloatField()
    fuel_on_arrival_gallons = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost = serializers.FloatField()


class AssumptionsSerializer(serializers.Serializer):
    mpg = serializers.FloatField()
    vehicle_range_miles = serializers.FloatField()
    tank_capacity_gallons = serializers.FloatField()
    start_fuel_gallons = serializers.FloatField()
    max_detour_miles = serializers.FloatField()
    stop_penalty = serializers.FloatField(allow_null=True)


class GeometrySerializer(serializers.Serializer):
    type = serializers.CharField(default="LineString")
    coordinates = serializers.ListField(child=serializers.ListField(child=serializers.FloatField()))


class RouteSerializer(serializers.Serializer):
    distance_miles = serializers.FloatField()
    duration_hours = serializers.FloatField()
    geometry = GeometrySerializer(help_text="GeoJSON LineString ([lng, lat] pairs).")


class SavingsComparisonSerializer(serializers.Serializer):
    baseline_cost = serializers.FloatField()
    amount = serializers.FloatField(help_text="Baseline cost minus plan cost (negative means the plan costs more).")
    percent = serializers.FloatField()


class SavingsSerializer(serializers.Serializer):
    vs_naive = SavingsComparisonSerializer(
        allow_null=True, help_text="Versus filling up only when the tank can't reach the next station."
    )
    vs_average_price = SavingsComparisonSerializer(
        allow_null=True, help_text="Versus buying the same gallons at the average price near the route."
    )
    average_price_near_route = serializers.FloatField(allow_null=True)
    per_gallon_vs_average = serializers.FloatField(allow_null=True, help_text="Dollars saved per gallon.")


class SummarySerializer(serializers.Serializer):
    total_fuel_cost = serializers.FloatField()
    total_gallons_purchased = serializers.FloatField()
    average_price_per_gallon = serializers.FloatField(allow_null=True)
    number_of_stops = serializers.IntegerField()
    stations_near_route = serializers.IntegerField()
    stations_considered = serializers.IntegerField(help_text="After keeping the cheapest station per town.")
    savings = SavingsSerializer()


class MetaSerializer(serializers.Serializer):
    routing_api_calls = serializers.IntegerField()
    routing_cache_hit = serializers.BooleanField()
    timings_ms = serializers.DictField(child=serializers.FloatField())


class RoutePlanResponseSerializer(serializers.Serializer):
    start = LocationSerializer()
    finish = LocationSerializer()
    strategy = serializers.CharField()
    assumptions = AssumptionsSerializer()
    route = RouteSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    summary = SummarySerializer()
    map_url = serializers.URLField(allow_null=True)
    meta = MetaSerializer()


class JobErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    detail = serializers.CharField()


class RoutePlanJobSerializer(serializers.ModelSerializer):
    status_url = serializers.SerializerMethodField()
    map_url = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = RoutePlanJob
        fields = [
            "id", "status", "params", "status_url", "map_url",
            "created_at", "started_at", "finished_at", "result", "error",
        ]

    def _url(self, name: str, job: RoutePlanJob) -> str:
        return self.context["request"].build_absolute_uri(reverse(name, args=[job.id]))

    def get_status_url(self, job: RoutePlanJob) -> str:
        return self._url("route-plan-job", job)

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_map_url(self, job: RoutePlanJob) -> str | None:
        return self._url("route-plan-job-map", job) if job.status == RoutePlanJob.Status.SUCCEEDED else None

    @extend_schema_field(RoutePlanResponseSerializer(allow_null=True))
    def get_result(self, job: RoutePlanJob) -> dict | None:
        if job.result is None:
            return None
        return {**job.result, "map_url": self.get_map_url(job)}

    @extend_schema_field(JobErrorSerializer(allow_null=True))
    def get_error(self, job: RoutePlanJob) -> dict | None:
        if job.status != RoutePlanJob.Status.FAILED:
            return None
        return {"status_code": job.error_status, "detail": job.error_detail}
