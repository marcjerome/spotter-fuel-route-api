import logging

from celery import shared_task
from celery.signals import worker_process_init
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from .jobs import ERROR_STATUS
from .models import RoutePlanJob
from .routing import RoutingError
from .services import PlanRequest, plan_route, serialize_result

logger = logging.getLogger(__name__)

ROUTING_RETRIES = 2  # the public OSRM servers have occasional multi-second stalls


@shared_task(bind=True, max_retries=ROUTING_RETRIES)
def run_route_plan_job(self, job_id: str) -> None:
    job = RoutePlanJob.objects.filter(pk=job_id).first()
    if job is None or job.is_finished:
        return  # deleted, or a redelivery of a job that already completed (acks_late)

    job.status = RoutePlanJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    try:
        result = plan_route(PlanRequest(**job.params))
        job.result = serialize_result(result)
        job.status = RoutePlanJob.Status.SUCCEEDED
    except RoutingError as exc:
        if not self.request.is_eager and self.request.retries < ROUTING_RETRIES:
            # A background job can wait out a flaky routing server; a sync request can't.
            job.status = RoutePlanJob.Status.PENDING
            job.save(update_fields=["status"])
            raise self.retry(exc=exc, countdown=5 * 2**self.request.retries)
        _fail(job, ERROR_STATUS[RoutingError], str(exc))
    except tuple(ERROR_STATUS) as exc:
        _fail(job, ERROR_STATUS[type(exc)], str(exc))
    except SoftTimeLimitExceeded:
        _fail(job, 504, "The route plan took too long to compute. Try the greedy strategy.")
    except Exception:
        logger.exception("Route plan job %s crashed", job_id)
        _fail(job, 500, "Unexpected error while planning the route.")

    job.finished_at = timezone.now()
    job.save(update_fields=["status", "result", "error_status", "error_detail", "finished_at"])


def _fail(job: RoutePlanJob, status: int, detail: str) -> None:
    job.status = RoutePlanJob.Status.FAILED
    job.error_status = status
    job.error_detail = detail


@worker_process_init.connect
def _warm_station_index(**kwargs) -> None:
    """Load the station index when a worker process starts, not on its first job."""
    from django.db import DatabaseError

    from .station_index import get_station_index

    try:
        get_station_index()
    except DatabaseError:
        logger.warning("Station index warm-up skipped", exc_info=True)
