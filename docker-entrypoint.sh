#!/bin/sh
set -e

# Only the web container prepares the database; workers just wait for it.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python manage.py migrate --noinput
    python manage.py createcachetable
    python manage.py load_stations --if-empty
fi

exec "$@"
