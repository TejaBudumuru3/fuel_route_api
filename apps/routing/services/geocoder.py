import time
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# OSM policy requires a descriptive User-Agent — do not omit this.
HEADERS = {
    "User-Agent": "FuelRouteOptimizer/1.0 (fuelroute@optimizer.dev)"
}

# Track last call time to respect the 1 req/sec rate limit
_last_call_time = 0.0


def geocode_address(address: str) -> tuple[float, float]:
    """
    Convert a US address string to (latitude, longitude).
    Returns standard (lat, lon) order — NOT ORS order.
    Raises ValueError if no result is found.

    Respects Nominatim's 1 request/second rate limit policy.
    """
    global _last_call_time

    # Enforce 1 req/sec rate limit
    elapsed = time.time() - _last_call_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
        "addressdetails": 0,
    }
    response = requests.get(
        NOMINATIM_URL, params=params,
        headers=HEADERS, timeout=10
    )
    _last_call_time = time.time()
    response.raise_for_status()
    results = response.json()

    if not results:
        raise ValueError(f"No geocoding result for: '{address}'")

    return float(results[0]["lat"]), float(results[0]["lon"])
