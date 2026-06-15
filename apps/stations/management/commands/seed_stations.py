"""
seed_stations — Seed gas stations from CSV and geocode ALL of them.

Strategy (completes in under 2 minutes, no long API waits):
  1. Load CSV, deduplicate by OPIS ID → ~6700 unique stations
  2. Bulk-insert all stations into the database
  3. Geocode ALL stations using a LOCAL US cities coordinates file
     (data/us_cities.csv — 29K cities, ~98% coverage)
  4. For any remaining unmatched cities (mostly Canadian),
     do a quick Nominatim lookup (only for unique cities, not per station)
"""
import time

import requests
import pandas as pd
from django.core.management.base import BaseCommand
from apps.stations.models import GasStation

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {
    "User-Agent": "FuelRouteOptimizer/1.0 (fuelroute@optimizer.dev)"
}

# Path to the bundled US cities database (relative to project root)
US_CITIES_CSV = "data/us_cities.csv"


class Command(BaseCommand):
    help = (
        "Seed gas stations from CSV and geocode ALL of them. "
        "Uses a local US cities database for fast geocoding (~2 min)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", type=str, required=True,
                            help="Path to the fuel prices CSV file")
        parser.add_argument("--force", action="store_true",
                            help="Drop all stations and re-seed from scratch")

    def handle(self, *args, **options):
        # ── Step 1: Load and aggregate ──────────────────────────────────
        self.stdout.write("Step 1: Loading CSV...")
        df = pd.read_csv(options["csv"], dtype=str)
        df.columns = ["opis_id", "name", "address", "city", "state",
                       "rack_id", "retail_price"]
        for col in ["name", "address", "city", "state"]:
            df[col] = df[col].str.strip()
        df["retail_price"] = pd.to_numeric(df["retail_price"], errors="coerce")
        df["rack_id"] = pd.to_numeric(df["rack_id"], errors="coerce").fillna(0)

        agg = df.groupby("opis_id").agg({
            "name":         "first",
            "address":      "first",
            "city":         "first",
            "state":        "first",
            "rack_id":      "first",
            "retail_price": "mean"
        }).reset_index()
        self.stdout.write(
            f"  {len(df)} rows reduced to {len(agg)} unique stations"
        )

        # ── Step 2: Insert stations into DB ─────────────────────────────
        self.stdout.write("Step 2: Inserting stations into DB...")
        if options["force"]:
            deleted, _ = GasStation.objects.all().delete()
            self.stdout.write(f"  --force: Deleted {deleted} existing stations")

        existing = set(
            GasStation.objects.values_list("opis_id", flat=True)
        )
        to_create = [
            GasStation(
                opis_id=int(r.opis_id),
                name=r.name,
                address=r.address,
                city=r.city,
                state=r.state,
                rack_id=int(r.rack_id),
                retail_price=round(r.retail_price, 5)
            )
            for r in agg.itertuples()
            if int(r.opis_id) not in existing
        ]
        if to_create:
            GasStation.objects.bulk_create(
                to_create, batch_size=500, ignore_conflicts=True
            )
        self.stdout.write(f"  Inserted {len(to_create)} stations")

        # ── Step 3: Geocode using local US cities database ──────────────
        self.stdout.write("Step 3: Geocoding via local cities database...")

        # Load the local cities lookup
        city_lookup = self._load_city_lookup()
        self.stdout.write(f"  Loaded {len(city_lookup)} city coordinates")

        # Get all un-geocoded stations (or all if --force)
        ungeocoded = list(GasStation.objects.filter(geocoded=False))
        if not ungeocoded:
            self.stdout.write(self.style.SUCCESS(
                "All stations already geocoded. Use --force to re-seed."
            ))
            return

        self.stdout.write(f"  {len(ungeocoded)} stations need geocoding")

        # Match against local lookup
        matched_objs = []
        unmatched_cities = {}  # {(city, state): [station_objs]}

        for station in ungeocoded:
            key = (station.city.upper().strip(),
                   station.state.upper().strip())
            if key in city_lookup:
                lat, lon = city_lookup[key]
                station.lat = lat
                station.lon = lon
                station.geocoded = True
                matched_objs.append(station)
            else:
                unmatched_cities.setdefault(key, []).append(station)

        # Bulk update matched stations
        if matched_objs:
            GasStation.objects.bulk_update(
                matched_objs, ["lat", "lon", "geocoded"], batch_size=500
            )
        self.stdout.write(
            f"  Local lookup matched {len(matched_objs)}/{len(ungeocoded)} "
            f"stations"
        )

        # ── Step 4: Nominatim fallback for remaining cities ─────────────
        if unmatched_cities:
            self.stdout.write(
                f"Step 4: Nominatim fallback for {len(unmatched_cities)} "
                f"unmatched cities ({sum(len(v) for v in unmatched_cities.values())} stations)..."
            )
            nominatim_matched = self._geocode_remaining(unmatched_cities)
            self.stdout.write(
                f"  Nominatim matched {nominatim_matched} more stations"
            )
        else:
            self.stdout.write("Step 4: No fallback needed — all cities matched!")

        # ── Summary ─────────────────────────────────────────────────────
        final_total = GasStation.objects.count()
        final_geocoded = GasStation.objects.filter(geocoded=True).count()
        final_remaining = final_total - final_geocoded

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! {final_geocoded}/{final_total} stations geocoded.\n"
            f"  Still ungeocoded: {final_remaining}"
        ))

    # ── Helpers ─────────────────────────────────────────────────────────

    def _load_city_lookup(self):
        """
        Load data/us_cities.csv into a dict:
          {(CITY_UPPER, STATE_CODE_UPPER): (lat, lon)}
        """
        try:
            cities_df = pd.read_csv(US_CITIES_CSV)
        except FileNotFoundError:
            self.stderr.write(
                f"ERROR: {US_CITIES_CSV} not found. "
                "Download it from: "
                "https://github.com/kelvins/US-Cities-Database"
            )
            return {}

        lookup = {}
        for _, row in cities_df.iterrows():
            key = (
                str(row["CITY"]).upper().strip(),
                str(row["STATE_CODE"]).upper().strip()
            )
            # First entry wins (avoid overwriting)
            if key not in lookup:
                lookup[key] = (float(row["LATITUDE"]),
                               float(row["LONGITUDE"]))
        return lookup

    def _geocode_remaining(self, unmatched_cities):
        """
        Geocode unmatched cities via Nominatim (one call per unique city).
        Typically only ~20-30 cities, finishes in under a minute.
        """
        total_matched = 0

        for i, ((city, state), stations) in enumerate(
            unmatched_cities.items(), 1
        ):
            # Try multiple query formats
            queries = [
                f"{city}, {state}, USA",
                f"{city}, {state}, Canada",
                f"{city}, {state}",
            ]

            lat, lon = None, None
            for query in queries:
                try:
                    lat, lon = self._nominatim_geocode(query)
                    break
                except (ValueError, requests.RequestException):
                    continue

            if lat is not None:
                # Update all stations in this city
                for s in stations:
                    s.lat = lat
                    s.lon = lon
                    s.geocoded = True
                GasStation.objects.bulk_update(
                    stations, ["lat", "lon", "geocoded"]
                )
                total_matched += len(stations)
            else:
                self.stderr.write(
                    f"  Could not geocode: {city}, {state} "
                    f"({len(stations)} stations)"
                )

            # Rate limit: 1 req/sec with margin
            time.sleep(1.5)

            if i % 10 == 0:
                self.stdout.write(
                    f"  [{i}/{len(unmatched_cities)}] "
                    f"{total_matched} stations matched so far"
                )

        return total_matched

    def _nominatim_geocode(self, query):
        """Geocode via Nominatim with retry on 429."""
        params = {
            "q": query,
            "format": "json",
            "limit": 1,
        }
        for attempt in range(3):
            response = requests.get(
                NOMINATIM_URL, params=params,
                headers=NOMINATIM_HEADERS, timeout=10
            )
            if response.status_code == 429:
                time.sleep(5 * (2 ** attempt))
                continue
            response.raise_for_status()
            results = response.json()

            if not results:
                raise ValueError(f"No result for: '{query}'")

            return float(results[0]["lat"]), float(results[0]["lon"])

        raise requests.RequestException(f"Rate limited: '{query}'")
