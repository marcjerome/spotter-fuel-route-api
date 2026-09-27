from django.urls import path

from .views import FuelStationListView

urlpatterns = [
    path("stations/", FuelStationListView.as_view(), name="station-list"),
]
