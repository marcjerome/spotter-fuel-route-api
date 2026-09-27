"""Django settings for the fuel route planner.

Configuration comes from environment variables (optionally loaded from a .env
file) so the same settings work locally, in tests, and in Docker.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "stations",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# PostgreSQL in Docker (DATABASE_URL set by docker-compose); SQLite otherwise.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

REDIS_URL = os.environ.get("REDIS_URL", "")

# Cached OSRM routes make repeat requests skip the external call entirely. The
# cache is shared by web and Celery workers: Redis when REDIS_URL is set
# (Docker), otherwise the database cache (run `manage.py createcachetable`).
# CACHE_BACKEND=locmem keeps it in process memory (used by the tests).
_CACHE_TIMEOUT = int(os.environ.get("ROUTE_CACHE_SECONDS", 60 * 60 * 24 * 7))
if os.environ.get("CACHE_BACKEND") == "locmem":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "TIMEOUT": _CACHE_TIMEOUT}}
elif REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": _CACHE_TIMEOUT,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "route_cache",
            "TIMEOUT": _CACHE_TIMEOUT,
            "OPTIONS": {"MAX_ENTRIES": 5000},
        }
    }

# Celery runs background route-plan jobs. Without REDIS_URL (local dev, tests)
# tasks run inline in the web process so the API behaves the same without a broker.
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not REDIS_URL)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_IGNORE_RESULT = True  # job state lives in the RoutePlanJob table
CELERY_TASK_ACKS_LATE = True  # a job whose worker dies is redelivered
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # long CPU jobs: take one at a time
CELERY_TASK_SOFT_TIME_LIMIT = 30
CELERY_TASK_TIME_LIMIT = 45
# CPU-bound MILP solves and I/O-bound routing jobs use separate queues so each
# can be given its own workers and concurrency.
CELERY_TASK_DEFAULT_QUEUE = "io"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    # Per-client limits on the expensive paths (see planner.throttles).
    "DEFAULT_THROTTLE_RATES": {
        "sync_milp": os.environ.get("THROTTLE_SYNC_MILP", "10/min"),
        "jobs": os.environ.get("THROTTLE_JOBS", "60/min"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Planner API",
    "DESCRIPTION": (
        "Plan a driving route between two US locations and get the cheapest set of "
        "fuel stops for a vehicle with a 500 mile range at 10 MPG."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Route planner configuration.
ROUTE_PLANNER = {
    # Tried in order; the next server is only called if the previous one fails.
    "OSRM_BASE_URLS": env_list(
        "OSRM_BASE_URLS", "https://routing.openstreetmap.de/routed-car,https://router.project-osrm.org"
    ),
    "OSRM_TIMEOUT_SECONDS": float(os.environ.get("OSRM_TIMEOUT_SECONDS", 10)),
    "VEHICLE_RANGE_MILES": 500.0,
    "VEHICLE_MPG": 10.0,
    "DEFAULT_MAX_DETOUR_MILES": 10.0,
    "DEFAULT_STRATEGY": "greedy",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
