from django.urls import path

from .views import RoutePlanJobMapView, RoutePlanJobView, RoutePlanMapView, RoutePlanView, RoutingStatsView

urlpatterns = [
    path("route-plan/", RoutePlanView.as_view(), name="route-plan"),
    path("route-plan/map/", RoutePlanMapView.as_view(), name="route-plan-map"),
    path("route-plan/jobs/<uuid:job_id>/", RoutePlanJobView.as_view(), name="route-plan-job"),
    path("stats/", RoutingStatsView.as_view(), name="routing-stats"),
    path("route-plan/jobs/<uuid:job_id>/map/", RoutePlanJobMapView.as_view(), name="route-plan-job-map"),
]
