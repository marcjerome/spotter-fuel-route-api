from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import caches
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .jobs import ERROR_STATUS, submit_job
from .locations import LocationError, resolve_location
from .models import RoutePlanJob
from .routing import get_stats
from .serializers import (
    RoutePlanJobSerializer,
    RoutePlanRequestSerializer,
    RoutePlanResponseSerializer,
)
from .services import PlanRequest, plan_route, serialize_result
from .throttles import JobSubmitThrottle, SyncMilpThrottle

ERROR_RESPONSES = {
    400: OpenApiResponse(description="Invalid input or unknown location."),
    422: OpenApiResponse(description="No feasible fuel plan (e.g. a gap longer than the vehicle range)."),
    429: OpenApiResponse(description="Rate limited (synchronous milp or job submissions)."),
    502: OpenApiResponse(description="The routing service failed."),
}


def _validated(params) -> dict:
    serializer = RoutePlanRequestSerializer(data=params)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _error(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=ERROR_STATUS[type(exc)])


def _plan_sync(request: Request, data: dict) -> tuple[dict | None, Response | None]:
    plan_params = {k: v for k, v in data.items() if k != "mode"}
    try:
        result = plan_route(PlanRequest(**plan_params))
    except tuple(ERROR_STATUS) as exc:
        return None, _error(exc)
    map_url = request.build_absolute_uri(f"{reverse('route-plan-map')}?{urlencode(plan_params)}")
    return serialize_result(result, map_url), None


def _job_payload(request: Request, job: RoutePlanJob) -> dict:
    return RoutePlanJobSerializer(job, context={"request": request}).data


class RoutePlanView(APIView):
    """Plan a route with the cheapest fuel stops."""

    throttle_classes = [SyncMilpThrottle, JobSubmitThrottle]

    def throttled(self, request, wait):
        raise Throttled(
            wait,
            detail="Rate limit reached. For milp, submit with \"mode\": \"async\" and poll the job instead.",
        )

    @extend_schema(
        summary="Plan a route and its fuel stops",
        description=(
            "Routes between two US locations (one routing API call; repeated requests are "
            "served from cache) and chooses where and how much to fuel up. Returns the route "
            "as GeoJSON, each fuel stop, the total fuel cost at 10 MPG, savings versus two "
            "baselines, and a link to an interactive map.\n\n"
            "With `mode=async` the plan is computed by a background worker instead: the "
            "response is `202 Accepted` with a job whose `status_url` returns the same plan "
            "once finished. Use it for the `milp` strategy or under heavy load."
        ),
        request=RoutePlanRequestSerializer,
        responses={
            200: RoutePlanResponseSerializer,
            202: OpenApiResponse(RoutePlanJobSerializer, description="Job accepted (mode=async)."),
            **ERROR_RESPONSES,
        },
        examples=[
            OpenApiExample(
                "New York to Los Angeles",
                value={"start": "New York, NY", "finish": "Los Angeles, CA"},
                request_only=True,
            ),
            OpenApiExample(
                "Fewer stops, computed in the background",
                value={
                    "start": "New York, NY",
                    "finish": "Los Angeles, CA",
                    "strategy": "milp",
                    "stop_penalty": 10,
                    "mode": "async",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Coordinates and a half tank",
                value={"start": "32.7767,-96.7970", "finish": "Chicago, IL", "start_fuel_gallons": 25},
                request_only=True,
            ),
        ],
    )
    def post(self, request: Request) -> Response:
        data = _validated(request.data)
        if data.pop("mode") == "async":
            return self._submit(request, data)
        payload, error = _plan_sync(request, data)
        return error or Response(payload)

    def _submit(self, request: Request, data: dict) -> Response:
        # Resolving locations is instant and offline, so bad input fails now
        # with a 400 instead of later inside the job.
        try:
            resolve_location(data["start"])
            resolve_location(data["finish"])
        except LocationError as exc:
            return _error(exc)
        job, created = submit_job(data)
        payload = _job_payload(request, job)
        return Response(
            payload,
            status=status.HTTP_202_ACCEPTED if created or not job.is_finished else status.HTTP_200_OK,
            headers={"Location": payload["status_url"]},
        )


class RoutePlanJobView(APIView):
    """Status and result of a background route plan."""

    @extend_schema(
        summary="Get a background route plan",
        description=(
            "Poll until `status` is `succeeded` (then `result` holds the same body as a "
            "synchronous plan) or `failed` (then `error` has the HTTP status and detail)."
        ),
        responses={200: RoutePlanJobSerializer, 404: OpenApiResponse(description="Unknown job.")},
    )
    def get(self, request: Request, job_id) -> Response:
        job = get_object_or_404(RoutePlanJob, pk=job_id)
        return Response(_job_payload(request, job))


@extend_schema(exclude=True)
class RoutePlanMapView(APIView):
    """Interactive Leaflet map of a planned route; reuses the cached route."""

    throttle_classes = [SyncMilpThrottle]

    def get(self, request: Request):
        data = _validated(request.query_params)
        payload, error = _plan_sync(request, data)
        if error:
            return render(
                request, "planner/map_error.html", {"detail": error.data["detail"]}, status=error.status_code
            )
        return render(request, "planner/map.html", {"plan": payload})


@extend_schema(exclude=True)
class RoutePlanJobMapView(APIView):
    """Map of a background job's stored result; refreshes itself while the job runs."""

    def get(self, request: Request, job_id):
        job = get_object_or_404(RoutePlanJob, pk=job_id)
        if job.status == RoutePlanJob.Status.SUCCEEDED:
            return render(request, "planner/map.html", {"plan": job.result})
        if job.status == RoutePlanJob.Status.FAILED:
            return render(request, "planner/map_error.html", {"detail": job.error_detail}, status=job.error_status)
        return render(request, "planner/map_error.html", {"detail": "Still planning this route...", "refresh": True})


class RoutingStatsView(APIView):
    """How often the external routing API is actually called."""

    @extend_schema(
        summary="Routing API usage and cache effectiveness",
        description=(
            "Running totals since the last `manage.py demo_reset`: HTTP calls made to the free "
            "OSRM routing API, and how many route lookups the cache answered instead."
        ),
        responses=inline_serializer(
            "RoutingStats",
            {
                "route_lookups": serializers.IntegerField(),
                "routing_api_calls": serializers.IntegerField(),
                "cache_hits": serializers.IntegerField(),
                "cache_misses": serializers.IntegerField(),
                "cache_hit_rate": serializers.FloatField(allow_null=True),
                "cache_backend": serializers.CharField(),
                "routing_servers": serializers.ListField(child=serializers.CharField()),
            },
        ),
    )
    def get(self, request: Request) -> Response:
        stats = get_stats()
        lookups = stats["cache_hits"] + stats["cache_misses"]
        return Response(
            {
                "route_lookups": lookups,
                "routing_api_calls": stats["api_calls"],
                "cache_hits": stats["cache_hits"],
                "cache_misses": stats["cache_misses"],
                "cache_hit_rate": round(stats["cache_hits"] / lookups, 3) if lookups else None,
                "cache_backend": type(caches["default"]).__name__,
                "routing_servers": settings.ROUTE_PLANNER["OSRM_BASE_URLS"],
            }
        )
