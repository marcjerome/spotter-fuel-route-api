from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics

from .models import FuelStation
from .serializers import FuelStationSerializer


@extend_schema(
    summary="List fuel stations",
    parameters=[
        OpenApiParameter("state", OpenApiTypes.STR, description="Two-letter state code, e.g. TX."),
        OpenApiParameter("city", OpenApiTypes.STR, description="City name (case-insensitive)."),
        OpenApiParameter("max_price", OpenApiTypes.DECIMAL, description="Only stations at or below this price."),
        OpenApiParameter("ordering", OpenApiTypes.STR, enum=["price", "-price"], description="Sort by price."),
    ],
)
class FuelStationListView(generics.ListAPIView):
    serializer_class = FuelStationSerializer

    def get_queryset(self):
        queryset = FuelStation.objects.all()
        params = self.request.query_params
        if state := params.get("state"):
            queryset = queryset.filter(state=state.upper())
        if city := params.get("city"):
            queryset = queryset.filter(city__iexact=city)
        if max_price := params.get("max_price"):
            queryset = queryset.filter(retail_price__lte=max_price)
        if params.get("ordering") in {"price", "-price"}:
            queryset = queryset.order_by(params["ordering"].replace("price", "retail_price"))
        return queryset
