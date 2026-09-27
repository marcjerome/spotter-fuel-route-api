from django.apps import AppConfig


class PlannerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "planner"

    def ready(self):
        # Build the offline city index at startup (~0.5s) rather than on the first request.
        from stations.geocoding import get_place_index

        get_place_index()
