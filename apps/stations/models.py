from django.db import models


class GasStation(models.Model):
    opis_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(default=0)
    retail_price = models.DecimalField(max_digits=8, decimal_places=5)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    geocoded = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "stations_gasstation"
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["lat", "lon"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"
