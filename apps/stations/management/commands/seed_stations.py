import csv
import io
import time

import requests
import pandas as pd
from django.core.management.base import BaseCommand
from apps.stations.models import GasStation

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Seed gas stations from CSV; geocode via US Census Batch API"

    def add_arguments(self, parser):
        parser.add_argument("--csv", type=str, required=True,
                            help="Path to the fuel prices CSV file")
        parser.add_argument("--force", action="store_true",
                            help="Re-geocode already-geocoded stations")

    def handle(self, *args, **options):
        # ── Step 1: Load and aggregate ──────────────────────────────────
        self.stdout.write("Step 1: Loading CSV...")
        df = pd.read_csv(options["csv"], dtype=str)
        df.columns = ["opis_id", "name", "address", "city", "state",
                       "rack_id", "retail_price"]
        # Strip whitespace from all string columns
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

        # ── Step 2: Bulk insert new stations ────────────────────────────
        self.stdout.write("Step 2: Inserting stations into DB...")
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
        GasStation.objects.bulk_create(
            to_create, batch_size=500, ignore_conflicts=True
        )
        self.stdout.write(f"  Inserted {len(to_create)} stations")

        # ── Step 3: Geocode via Census Batch API ────────────────────────
        if options["force"]:
            qs = GasStation.objects.all()
        else:
            qs = GasStation.objects.filter(geocoded=False)
        stations = list(
            qs.values("id", "opis_id", "address", "city", "state")
        )

        if not stations:
            self.stdout.write("All stations already geocoded.")
            return

        self.stdout.write(
            f"Step 3: Geocoding {len(stations)} stations via Census API..."
        )
        batches = [
            stations[i:i + BATCH_SIZE]
            for i in range(0, len(stations), BATCH_SIZE)
        ]
        total_matched = 0

        for i, batch in enumerate(batches, 1):
            self.stdout.write(
                f"  Batch {i}/{len(batches)} ({len(batch)} records)..."
            )
            csv_buf = self._build_census_csv(batch)
            try:
                response_text = self._post_census(csv_buf)
                matched = self._apply_geocode_results(response_text)
                total_matched += matched
                self.stdout.write(f"    Matched {matched} stations in batch")
            except requests.RequestException as e:
                self.stderr.write(
                    f"    Census API error on batch {i}: {e}"
                )
            time.sleep(0.5)

        self.stdout.write(self.style.SUCCESS(
            f"Done. {total_matched}/{len(stations)} stations geocoded."
        ))

    # ── Helpers ─────────────────────────────────────────────────────────

    def _build_census_csv(self, stations):
        """
        Build a Census-format CSV:
        Unique ID, Street Address, City, State, ZIP (blank)
        """
        buf = io.StringIO()
        w = csv.writer(buf)
        for s in stations:
            w.writerow([
                s["opis_id"], s["address"], s["city"], s["state"], ""
            ])
        return buf.getvalue()

    def _post_census(self, csv_content, retries=3):
        """
        POST to the Census Batch Geocoder with retry logic.
        """
        for attempt in range(retries):
            try:
                r = requests.post(
                    CENSUS_URL,
                    files={
                        "addressFile": (
                            "batch.csv", csv_content, "text/plain"
                        )
                    },
                    data={
                        "benchmark": "Public_AR_Current",
                        "returntype": "locations"
                    },
                    timeout=120
                )
                r.raise_for_status()
                return r.text
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def _apply_geocode_results(self, response_text):
        """
        Parse Census Batch Geocoder response.

        Census response row structure (after CSV split):
          [0] OPIS ID
          [1] Input address
          [2] Match status ("Match" / "No_Match" / "Tie")
          [3] Match precision ("Exact" / "Non_Exact")
          [4] Matched address string
          [5] Coordinates — "longitude,latitude" sub-string
          [6] Tiger/Line ID
          [7] Street side

        IMPORTANT: Column [5] contains "longitude,latitude" as a
        comma-separated sub-string. When splitting the full row by comma
        naively, the coordinate sub-string splits across columns [5] and [6].
        Treat columns [5] and [6] as longitude and latitude respectively.
        """
        updates = []
        for line in response_text.strip().split("\n"):
            if not line.strip():
                continue
            # Use csv.reader to handle quoted fields properly
            try:
                reader = csv.reader(io.StringIO(line))
                parts = next(reader)
            except (csv.Error, StopIteration):
                continue

            if len(parts) < 8:
                continue

            # Column 2 is match status
            if parts[2].strip().strip('"').upper() != "MATCH":
                continue

            try:
                # Column 5 is the coordinates field: "lon,lat"
                # The CSV reader may keep it as one field or split it
                coord_str = parts[5].strip().strip('"')
                if "," in coord_str:
                    # Coordinates are in a single field: "lon,lat"
                    lon_str, lat_str = coord_str.split(",", 1)
                    lon = float(lon_str.strip())
                    lat = float(lat_str.strip())
                else:
                    # Coordinates were split by naive CSV parsing
                    lon = float(parts[5].strip())
                    lat = float(parts[6].strip())
            except (ValueError, IndexError):
                continue

            opis_id_str = parts[0].strip().strip('"')
            try:
                opis_id = int(opis_id_str)
            except ValueError:
                continue

            updates.append({
                "opis_id": opis_id,
                "lat": lat,
                "lon": lon
            })

        if not updates:
            return 0

        id_map = {u["opis_id"]: u for u in updates}
        objs = list(
            GasStation.objects.filter(opis_id__in=list(id_map.keys()))
        )
        for obj in objs:
            u = id_map[obj.opis_id]
            obj.lat = u["lat"]
            obj.lon = u["lon"]
            obj.geocoded = True
        GasStation.objects.bulk_update(objs, ["lat", "lon", "geocoded"])
        return len(objs)
