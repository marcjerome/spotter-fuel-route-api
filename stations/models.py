from django.db import models


class FuelStation(models.Model):
    """A truck stop from the provided fuel price sheet.

    Coordinates are the centroid of the station's city (see stations.geocoding),
    so they are approximate to within a few miles. Rows whose city could not be
    geocoded keep null coordinates and are ignored by the route planner.
    """

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=8, decimal_places=5)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["state", "city", "name"]
        indexes = [models.Index(fields=["retail_price"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None
