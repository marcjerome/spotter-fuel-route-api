"""Test settings: in-memory cache, inline Celery tasks, no external services."""

from .settings import *  # noqa: F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_BROKER_URL = "memory://"
CELERY_TASK_ALWAYS_EAGER = True
