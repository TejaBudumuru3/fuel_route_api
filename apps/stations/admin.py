from django.contrib import admin
from .models import GasStation


@admin.register(GasStation)
class GasStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "geocoded")
    list_filter = ("state", "geocoded")
    search_fields = ("name", "city", "opis_id")
    readonly_fields = ("opis_id",)
