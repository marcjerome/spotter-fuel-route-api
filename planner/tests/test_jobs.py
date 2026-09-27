from unittest import mock

import pytest
from celery.exceptions import Retry
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from planner import services
from planner.jobs import queue_for
from planner.models import RoutePlanJob
from planner.routing import RoutingError
from planner.tasks import run_route_plan_job

from .test_api import api, routing_calls, stations  # noqa: F401  (shared fixtures)

URL = reverse("route-plan")
TRIP = {"start": "Oklahoma City, OK", "finish": "Nashville, TN"}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()  # throttle counters live in the cache
    yield
    cache.clear()


def submit(api, captured, **extra):
    with captured(execute=True):
        return api.post(URL, {**TRIP, "mode": "async", **extra}, format="json")


@pytest.mark.django_db
class TestAsyncJobs:
    def test_submit_returns_202_and_job_completes(
        self, api, stations, routing_calls, django_capture_on_commit_callbacks
    ):
        response = submit(api, django_capture_on_commit_callbacks, strategy="milp", stop_penalty=10)
        assert response.status_code == 202, response.content
        job = response.json()
        assert response["Location"] == job["status_url"]
        assert job["params"]["strategy"] == "milp"
        assert "mode" not in job["params"]

        # Tests run Celery tasks eagerly, so the job has finished by now.
        polled = api.get(job["status_url"]).json()
        assert polled["status"] == "succeeded"
        assert polled["error"] is None
        result = polled["result"]
        assert result["strategy"] == "milp"
        assert result["summary"]["total_gallons_purchased"] == pytest.approx(18.0, abs=0.01)
        assert result["map_url"] == polled["map_url"]
        assert polled["map_url"].endswith(f"/route-plan/jobs/{job['id']}/map/")

    def test_identical_requests_share_one_job(
        self, api, stations, routing_calls, django_capture_on_commit_callbacks
    ):
        first = submit(api, django_capture_on_commit_callbacks).json()
        second = submit(api, django_capture_on_commit_callbacks)
        assert second.json()["id"] == first["id"]
        assert second.status_code == 200  # already finished, served from the stored result
        assert RoutePlanJob.objects.count() == 1
        assert len(routing_calls) == 1

    def test_different_params_get_different_jobs(
        self, api, stations, routing_calls, django_capture_on_commit_callbacks
    ):
        a = submit(api, django_capture_on_commit_callbacks).json()
        b = submit(api, django_capture_on_commit_callbacks, start_fuel_gallons=10).json()
        assert a["id"] != b["id"]

    def test_bad_location_fails_fast_without_a_job(self, api, stations, routing_calls):
        response = api.post(URL, {**TRIP, "start": "Atlantis, ZZ", "mode": "async"}, format="json")
        assert response.status_code == 400
        assert RoutePlanJob.objects.count() == 0

    def test_routing_failure_is_recorded_on_the_job(
        self, api, stations, monkeypatch, django_capture_on_commit_callbacks
    ):
        def broken(start, finish):
            raise RoutingError("The routing service is unreachable. Please try again.")

        monkeypatch.setattr(services, "get_route", broken)
        job = submit(api, django_capture_on_commit_callbacks).json()
        polled = api.get(job["status_url"]).json()
        assert polled["status"] == "failed"
        assert polled["error"] == {"status_code": 502, "detail": "The routing service is unreachable. Please try again."}
        assert polled["result"] is None

    def test_unknown_job_404(self, api, db):
        response = api.get(reverse("route-plan-job", args=["00000000-0000-0000-0000-000000000000"]))
        assert response.status_code == 404

    def test_job_map_page(self, api, stations, routing_calls, django_capture_on_commit_callbacks):
        job = submit(api, django_capture_on_commit_callbacks).json()
        response = api.get(reverse("route-plan-job-map", args=[job["id"]]))
        assert response.status_code == 200
        assert 'id="plan-data"' in response.content.decode()

    def test_pending_job_map_page_refreshes(self, api, db):
        job = RoutePlanJob.objects.create(params=TRIP, params_hash="x")
        response = api.get(reverse("route-plan-job-map", args=[job.id]))
        assert response.status_code == 200
        assert 'http-equiv="refresh"' in response.content.decode()

    def test_enqueued_on_the_right_queue(self, api, stations, django_capture_on_commit_callbacks):
        with mock.patch("planner.tasks.run_route_plan_job.apply_async") as apply_async:
            submit(api, django_capture_on_commit_callbacks, strategy="milp")
            submit(api, django_capture_on_commit_callbacks, strategy="greedy")
        assert [c.kwargs["queue"] for c in apply_async.call_args_list] == ["cpu", "io"]


def test_queue_routing():
    assert queue_for({"strategy": "milp"}) == "cpu"
    assert queue_for({"strategy": "greedy"}) == "io"


@pytest.mark.django_db
class TestRoutingRetries:
    """On a real worker, routing failures are retried with backoff before failing."""

    @pytest.fixture
    def broken_routing(self, monkeypatch):
        def broken(start, finish):
            raise RoutingError("timeout")

        monkeypatch.setattr(services, "get_route", broken)

    def test_first_failure_schedules_a_retry(self, stations, broken_routing):
        job = RoutePlanJob.objects.create(params=TRIP, params_hash="x")
        with mock.patch.object(run_route_plan_job, "retry", side_effect=Retry()) as retry:
            with pytest.raises(Retry):
                run_route_plan_job(str(job.id))
        assert retry.call_args.kwargs["countdown"] == 5
        job.refresh_from_db()
        assert job.status == RoutePlanJob.Status.PENDING

    def test_fails_once_retries_are_exhausted(self, stations, broken_routing):
        job = RoutePlanJob.objects.create(params=TRIP, params_hash="x")
        run_route_plan_job.push_request(retries=2)
        try:
            run_route_plan_job(str(job.id))
        finally:
            run_route_plan_job.pop_request()
        job.refresh_from_db()
        assert (job.status, job.error_status) == (RoutePlanJob.Status.FAILED, 502)


@pytest.mark.django_db
def test_task_skips_finished_jobs(stations, routing_calls):
    """A redelivered message (acks_late) must not recompute a finished job."""
    job = RoutePlanJob.objects.create(params=TRIP, params_hash="x", status=RoutePlanJob.Status.SUCCEEDED)
    run_route_plan_job(str(job.id))
    assert routing_calls == []


@pytest.mark.django_db
class TestThrottling:
    @override_settings(
        REST_FRAMEWORK={
            "DEFAULT_THROTTLE_RATES": {"sync_milp": "2/min", "jobs": "100/min"},
            "DEFAULT_AUTHENTICATION_CLASSES": [],
            "UNAUTHENTICATED_USER": None,
        }
    )
    def test_sync_milp_is_throttled_but_greedy_and_async_are_not(
        self, api, stations, routing_calls, django_capture_on_commit_callbacks
    ):
        from rest_framework.settings import api_settings
        from rest_framework.throttling import SimpleRateThrottle

        with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", api_settings.DEFAULT_THROTTLE_RATES):
            milp = {**TRIP, "strategy": "milp"}
            assert api.post(URL, milp, format="json").status_code == 200
            assert api.post(URL, milp, format="json").status_code == 200
            blocked = api.post(URL, milp, format="json")
            assert blocked.status_code == 429
            assert "async" in blocked.json()["detail"]

            assert api.post(URL, TRIP, format="json").status_code == 200  # greedy unaffected
            assert submit(api, django_capture_on_commit_callbacks, strategy="milp").status_code == 202
