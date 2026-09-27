import hashlib
import json
import uuid

from django.db import models


class RoutePlanJob(models.Model):
    """A route plan computed in the background by a Celery worker.

    The job row is the source of truth for status and result, so clients can
    poll it, it survives worker restarts, and identical requests can share one
    computation through `params_hash`.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    params = models.JSONField()
    params_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    result = models.JSONField(null=True, blank=True)
    error_status = models.PositiveSmallIntegerField(null=True, blank=True, help_text="HTTP status of the failure.")
    error_detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.params.get('start')} -> {self.params.get('finish')} ({self.status})"

    @staticmethod
    def hash_params(params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()

    @property
    def is_finished(self) -> bool:
        return self.status in {self.Status.SUCCEEDED, self.Status.FAILED}
