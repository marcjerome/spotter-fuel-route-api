"""Submitting route-plan jobs to the Celery workers."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .locations import LocationError
from .models import RoutePlanJob
from .optimizers import InfeasibleRouteError
from .routing import RoutingError

# How long a finished result is reused for an identical request.
RESULT_REUSE = timedelta(hours=1)

ERROR_STATUS = {LocationError: 400, InfeasibleRouteError: 422, RoutingError: 502}


def queue_for(params: dict) -> str:
    """MILP solves are CPU-bound; everything else mostly waits on OSRM."""
    return "cpu" if params.get("strategy") == "milp" else "io"


def submit_job(params: dict) -> tuple[RoutePlanJob, bool]:
    """Create and enqueue a job, or return an identical one in flight or recently finished.

    Returns (job, created). Deduplicating on the input hash means many drivers
    requesting the same trip share a single computation.
    """
    params_hash = RoutePlanJob.hash_params(params)
    existing = (
        RoutePlanJob.objects.filter(params_hash=params_hash)
        .filter(
            Q(status__in=[RoutePlanJob.Status.PENDING, RoutePlanJob.Status.RUNNING])
            | Q(status=RoutePlanJob.Status.SUCCEEDED, finished_at__gte=timezone.now() - RESULT_REUSE)
        )
        .first()
    )
    if existing:
        return existing, False

    job = RoutePlanJob.objects.create(params=params, params_hash=params_hash)
    # Enqueue only once the row is committed, so the worker can always read it.
    from .tasks import run_route_plan_job

    transaction.on_commit(lambda: run_route_plan_job.apply_async(args=[str(job.id)], queue=queue_for(params)))
    return job, True
