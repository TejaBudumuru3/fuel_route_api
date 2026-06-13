import requests
from django.conf import settings

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"


def get_route(start_lat: float, start_lon: float,
              end_lat: float, end_lon: float) -> dict:
    """
    Fetch a driving route from OpenRouteService.

    COORDINATE ORDER WARNING:
    ORS requires [longitude, latitude] — the REVERSE of standard notation.
    Pass standard (lat, lon) to this function; the conversion is done here.

    Returns:
        {
          "coordinates":       [[lon, lat], ...],  # ORS [lon,lat] order preserved
          "distance_meters":   float,
          "duration_seconds":  float
        }
    """
    payload = {
        "coordinates": [
            [start_lon, start_lat],   # ORS order: [lon, lat]
            [end_lon, end_lat]
        ]
    }
    headers = {
        "Authorization": settings.ORS_API_KEY,
        "Content-Type": "application/json"
    }
    response = requests.post(
        ORS_URL, json=payload,
        headers=headers, timeout=30
    )
    response.raise_for_status()

    data = response.json()
    features = data.get("features", [])
    if not features:
        raise ValueError("ORS returned no route features.")

    geometry = features[0]["geometry"]
    summary = features[0]["properties"]["summary"]

    return {
        "coordinates": geometry["coordinates"],      # [[lon, lat], ...]
        "distance_meters": summary["distance"],
        "duration_seconds": summary["duration"],
    }
