import time
import requests
from django.core.management.base import BaseCommand
from apps.stations.models import GasStation

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {
    "User-Agent": "FuelRouteOptimizer/1.0 (fuelroute@optimizer.dev)"
}


class Command(BaseCommand):
    help = (
        "Fallback geocoder: uses Nominatim to geocode un-geocoded stations "
        "by 'City, State' lookup. Groups stations by city/state to minimize "
        "API calls (one per unique city instead of one per station)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Max number of unique cities to geocode (0 = all)"
        )

    def handle(self, *args, **options):
        # Get all un-geocoded stations
        ungeocoded = GasStation.objects.filter(geocoded=False)
        total = ungeocoded.count()
        if total == 0:
            self.stdout.write("All stations already geocoded.")
            return

        self.stdout.write(f"Found {total} un-geocoded stations.")

        # Group by city+state to minimize Nominatim calls
        city_state_pairs = (
            ungeocoded
            .values_list("city", "state")
            .distinct()
        )
        city_state_list = list(city_state_pairs)
        self.stdout.write(
            f"Grouped into {len(city_state_list)} unique city/state pairs."
        )

        limit = options["limit"]
        if limit > 0:
            city_state_list = city_state_list[:limit]
            self.stdout.write(f"  (Limited to {limit} cities)")

        geocoded_count = 0
        failed_cities = 0

        for i, (city, state) in enumerate(city_state_list, 1):
            query = f"{city}, {state}, USA"
            try:
                lat, lon = self._nominatim_geocode(query)

                # Update all stations in this city/state
                updated = GasStation.objects.filter(
                    geocoded=False, city=city, state=state
                ).update(lat=lat, lon=lon, geocoded=True)

                geocoded_count += updated
                if i % 50 == 0 or i == len(city_state_list):
                    self.stdout.write(
                        f"  [{i}/{len(city_state_list)}] "
                        f"Geocoded {updated} stations in {city}, {state} "
                        f"({geocoded_count} total)"
                    )
            except (ValueError, requests.RequestException) as e:
                failed_cities += 1
                if i % 100 == 0:
                    self.stderr.write(
                        f"  [{i}] Failed: {query} — {e}"
                    )

            # Respect Nominatim 1 req/sec rate limit
            time.sleep(1.05)

        self.stdout.write(self.style.SUCCESS(
            f"Done. Geocoded {geocoded_count} stations across "
            f"{len(city_state_list) - failed_cities} cities. "
            f"Failed cities: {failed_cities}."
        ))

    def _nominatim_geocode(self, query: str) -> tuple[float, float]:
        """Geocode a location string via Nominatim."""
        params = {
            "q": query,
            "format": "json",
            "limit": 1,
            "countrycodes": "us",
        }
        response = requests.get(
            NOMINATIM_URL, params=params,
            headers=HEADERS, timeout=10
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            raise ValueError(f"No result for: '{query}'")

        return float(results[0]["lat"]), float(results[0]["lon"])
