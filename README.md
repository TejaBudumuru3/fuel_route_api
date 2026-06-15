# Fuel Route Optimizer

A Django REST API that finds the cheapest fuel stops along any driving route in the US. Give it a start and end location, and it returns the full route geometry with optimized refueling points based on real station prices from the provided dataset.

## How it works

The system has two phases: a one-time data seeding step, and the runtime API.

**Seeding (runs once, ~2 min):** The management command reads the fuel prices CSV, deduplicates the 8,000+ rows down to ~6,700 unique stations by OPIS ID (averaging prices across duplicates), bulk-inserts them into the database, and geocodes each one using a bundled US cities coordinate file. Any cities not found locally get a Nominatim fallback lookup. After seeding, every station has lat/lon coordinates ready for spatial queries.

**Runtime (per request):** When a user POSTs a start/end location pair, the API:

1. Checks the route cache — if this exact query was computed in the last 24 hours, returns instantly (~40ms).
2. Geocodes both addresses via Nominatim (2 calls, respecting their 1 req/sec rate limit).
3. Fetches the driving route from OpenRouteService (1 call). This is the only routing API call per request.
4. Runs a two-pass spatial filter to find stations near the route — first a fast bounding-box query against the DB indexes, then Haversine distance checks to keep only stations within 10 miles of the highway corridor.
5. Runs a greedy look-ahead fuel optimizer that decides where to stop and how much to buy at each station. It looks 500 miles ahead: if a cheaper station is reachable, it buys just enough to get there; if not, it fills the tank.
6. Returns a GeoJSON response containing the route polyline, each fuel stop as a Point feature, and a cost summary.

**Assumptions:** The vehicle starts with a full 50-gallon tank, gets 10 MPG, and has a max range of 500 miles per fill-up.

## Setup

Tested on Python 3.12. Should work on 3.10+.

```bash
# Clone and enter the project
cd fuel_route_api

# Create virtualenv and install deps
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Copy the env template and fill in your values
cp .env.example .env
# Edit .env — you need an ORS API key (free at https://openrouteservice.org/dev/#/login)
# Generate a Django secret key: python -c "import secrets; print(secrets.token_hex(32))"

# Run migrations
python manage.py migrate

# Seed the station database (~2 minutes)
python manage.py seed_stations --csv data/fuel-prices-for-be-assessment.csv

# Start the dev server
python manage.py runserver
```

If you need to re-seed from scratch (drops all existing stations):
```bash
python manage.py seed_stations --csv data/fuel-prices-for-be-assessment.csv --force
```

## API Usage

### `POST /api/route/`

**Request:**
```json
{
  "start": "New York, NY",
  "end": "Los Angeles, CA"
}
```

**Response (abbreviated):**
```json
{
  "route": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": {
          "type": "LineString",
          "coordinates": [[-74.006, 40.713], "..."]
        },
        "properties": {
          "total_distance_miles": 2793.6,
          "total_duration_hours": 44.9
        }
      },
      {
        "type": "Feature",
        "geometry": {
          "type": "Point",
          "coordinates": [-83.53, 41.64]
        },
        "properties": {
          "stop_number": 1,
          "name": "S&G #88",
          "city": "Toledo",
          "state": "OH",
          "price_per_gallon": 3.009,
          "gallons_purchased": 42.24,
          "cost_at_stop": 127.11,
          "miles_from_start": 560.1,
          "tank_after_stop": 43.78
        }
      }
    ]
  },
  "summary": {
    "total_distance_miles": 2793.6,
    "total_duration_hours": 44.9,
    "total_fuel_stops": 11,
    "total_gallons_purchased": 251.83,
    "total_fuel_cost_usd": 779.71
  }
}
```

The first feature is always the route LineString. Every subsequent feature is a fuel stop Point. Coordinates follow GeoJSON convention: `[longitude, latitude]`.

### Error responses

| Situation | Status | Example |
|-----------|--------|---------|
| Missing or blank field | 400 | `{"end": ["This field is required."]}` |
| Same start and end | 400 | `{"end": ["Start and end locations must be different."]}` |
| Unrecognized location | 400 | `{"error": "Could not geocode start location: ..."}` |
| No stations along route | 500 | `{"error": "No fuel stations found along this route."}` |

## Project structure

```
fuel_route_api/
├── manage.py
├── requirements.txt
├── .env.example
├── data/
│   ├── fuel-prices-for-be-assessment.csv    # Provided fuel prices
│   └── us_cities.csv                        # ~29K US city coordinates (for offline geocoding)
├── config/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── apps/
    ├── stations/
    │   ├── models.py                        # GasStation model
    │   ├── admin.py
    │   └── management/commands/
    │       ├── seed_stations.py             # CSV ingest + geocoding pipeline
    │       └── geocode_fallback.py          # Standalone Nominatim batch geocoder
    └── routing/
        ├── models.py                        # RouteCache model
        ├── serializers.py                   # Input validation
        ├── views.py                         # POST /api/route/ handler
        ├── urls.py
        └── services/
            ├── geocoder.py                  # Nominatim address → lat/lon
            ├── ors_client.py                # OpenRouteService route fetching
            ├── corridor.py                  # Bounding box + Haversine spatial filter
            └── optimizer.py                 # Greedy fuel stop selection
```

## External APIs used

| API | Purpose | Calls per request | Auth |
|-----|---------|-------------------|------|
| [Nominatim](https://nominatim.openstreetmap.org) | Convert addresses to coordinates | 2 (start + end) | None (just a User-Agent header) |
| [OpenRouteService](https://openrouteservice.org) | Driving route geometry | 1 | Free API key (2,000 req/day) |

The seeding command uses a local US cities CSV file for geocoding stations, so there are zero external API calls during the data import step beyond a small Nominatim fallback for cities not in the local file.

## Key design decisions

**Why a local cities file instead of Census geocoding?** The fuel prices CSV uses highway exit descriptions as addresses (e.g., "I-44, EXIT 283 & US-69"), not proper street addresses. The US Census Batch Geocoder only matched about 8% of them. Using a local cities coordinate file gets ~98% coverage instantly without any API calls.

**Why SQLite?** It's zero-config and bundled with Python. For this dataset size (~6,700 stations), SQLite handles the bounding-box queries in under 5ms. The lat/lon composite index makes spatial pre-filtering fast enough. Swapping to PostgreSQL is just a settings change if needed.

**Why cache in the DB?** Route responses are cached in a `RouteCache` table keyed by SHA-256 of the normalized input. Repeat queries return in ~40ms with no external API calls. The cache expires after 24 hours. A database-backed cache survives server restarts, which an in-memory cache wouldn't.

**The optimizer logic:** It's a greedy algorithm with a 500-mile look-ahead window. At each candidate station, it checks if there's a cheaper station reachable within the remaining tank range. If yes, it buys just enough fuel (plus a 5% safety buffer) to reach the cheaper one. If not, it fills up. A minimum purchase threshold of 2 gallons prevents the algorithm from making trivial 1-gallon pit stops.

## Performance

| Scenario | Response time | External calls |
|----------|--------------|----------------|
| Cached query | ~40ms | 0 |
| Fresh short route (<500 mi) | ~3s | 3 |
| Fresh cross-country route | ~5s | 3 |

All latency on fresh requests comes from Nominatim and ORS network calls. The spatial filter and optimizer combined take under 50ms of CPU time.

## Tech stack

- Django 5.1.4
- Django REST Framework 3.15.2
- pandas (CSV processing)
- requests (HTTP client)
- python-decouple (env management)
- SQLite (dev) / PostgreSQL-ready (prod)
