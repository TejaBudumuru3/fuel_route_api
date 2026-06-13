from django.db import models
from django.utils import timezone
from datetime import timedelta


class RouteCache(models.Model):
    cache_key = models.CharField(max_length=64, unique=True, db_index=True)
    start_input = models.CharField(max_length=255)
    end_input = models.CharField(max_length=255)
    response_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "routing_routecache"

    def is_expired(self):
        return (timezone.now() - self.created_at) > timedelta(hours=24)

    def __str__(self):
        return f"Cache: {self.start_input} → {self.end_input}"
