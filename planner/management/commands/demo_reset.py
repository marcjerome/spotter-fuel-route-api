from django.core.cache import cache
from django.core.management.base import BaseCommand

from planner.models import RoutePlanJob


class Command(BaseCommand):
    help = "Start a demo from a clean slate: clear cached routes, usage counters and background jobs."

    def handle(self, *args, **options):
        cache.clear()  # cached routes, routing stats, throttle counters
        jobs, _ = RoutePlanJob.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"Cleared the route cache and stats, and deleted {jobs} jobs."))
