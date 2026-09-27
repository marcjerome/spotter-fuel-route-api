"""WSGI entry point.

Also warms the in-memory station index so no request pays the cold-start cost.
Under `gunicorn --preload` this runs once in the master process and the forked
workers share the loaded data copy-on-write.
"""

import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()


def _warm_up() -> None:
    from django.db import DatabaseError, connections

    from planner.station_index import get_station_index

    try:
        get_station_index()
    except DatabaseError:  # e.g. tables not migrated yet; the index loads lazily instead
        logging.getLogger(__name__).warning("Station index warm-up skipped", exc_info=True)
    finally:
        connections.close_all()  # never share a DB connection across forked workers


_warm_up()
