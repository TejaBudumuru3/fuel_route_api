import hashlib
import logging
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .serializers import RouteRequestSerializer
from .services.geocoder import geocode_address
from .services.ors_client import get_route
from .services.corridor import build_route_checkpoints, find_stations_along_route
from .services.optimizer import optimize_fuel_stops
from .models import RouteCache

logger = logging.getLogger(__name__)

M_TO_MI = 0.000621371
S_TO_H = 1 / 3600


def _cache_key(start: str, end: str) -> str:
    """
    Generate a deterministic SHA-256 cache key from start/end inputs.
    Case-insensitive and whitespace-normalized.
    """
    raw = f"{start.strip().lower()}:{end.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _build_response(route_coords, stops, total_mi, total_h, total_cost):
    """
    Construct the GeoJSON FeatureCollection response.

    Feature 0: Route LineString (the full driving path)
    Features 1..N: Fuel Stop Points
    """
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": route_coords
            },
            "properties": {
                "total_distance_miles": round(total_mi, 1),
                "total_duration_hours": round(total_h, 2)
            }
        }
    ]
    for s in stops:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [s.lon, s.lat]   # GeoJSON: [lon, lat]
            },
            "properties": {
                "stop_number": s.stop_number,
                "name": s.name,
                "city": s.city,
                "state": s.state,
                "price_per_gallon": s.price_per_gallon,
                "gallons_purchased": s.gallons_purchased,
                "cost_at_stop": s.cost_at_stop,
                "miles_from_start": s.miles_from_start,
                "tank_after_stop": s.tank_after_stop,
            }
        })
    total_gal = round(sum(s.gallons_purchased for s in stops), 2)
    return {
        "route": {
            "type": "FeatureCollection",
            "features": features
        },
        "summary": {
            "total_distance_miles": round(total_mi, 1),
            "total_duration_hours": round(total_h, 2),
            "total_fuel_stops": len(stops),
            "total_gallons_purchased": total_gal,
            "total_fuel_cost_usd": total_cost
        }
    }


class RouteView(APIView):
    """
    POST /api/route/

    Accepts start and end locations (US addresses or city names),
    computes the driving route, finds optimal fuel stops along the
    corridor, and returns a GeoJSON response with cost breakdown.

    External API calls per fresh request:
        - 2x Nominatim (start + end geocoding)
        - 1x OpenRouteService (driving route)
        - 0x Census (stations pre-seeded)
    Total: 3 external calls (within the "2 or 3 is acceptable" constraint)
    """

    def post(self, request):
        # ── Validate input ──────────────────────────────────────────────
        ser = RouteRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        start = ser.validated_data["start"]
        end = ser.validated_data["end"]
        key = _cache_key(start, end)

        # ── Cache check ─────────────────────────────────────────────────
        try:
            cached = RouteCache.objects.filter(cache_key=key).first()
            if cached and not cached.is_expired():
                logger.info("Cache hit for %s -> %s", start, end)
                return Response(cached.response_data)
        except Exception:
            pass  # If cache lookup fails, proceed with fresh compute

        # ── Geocode start location ──────────────────────────────────────
        try:
            start_lat, start_lon = geocode_address(start)
        except ValueError as e:
            return Response(
                {"error": f"Could not geocode start location: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except requests.RequestException as e:
            return Response(
                {"error": f"Could not geocode start location: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Geocode end location ────────────────────────────────────────
        try:
            end_lat, end_lon = geocode_address(end)
        except ValueError as e:
            return Response(
                {"error": f"Could not geocode end location: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except requests.RequestException as e:
            return Response(
                {"error": f"Could not geocode end location: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Get route from ORS (single API call) ────────────────────────
        try:
            route = get_route(start_lat, start_lon, end_lat, end_lon)
        except ValueError as e:
            return Response(
                {"error": f"Could not compute route: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except requests.RequestException as e:
            return Response(
                {"error": f"Could not compute route: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        total_mi = route["distance_meters"] * M_TO_MI
        total_h = route["duration_seconds"] * S_TO_H

        # ── Spatial filter (bounding box + Haversine) ───────────────────
        checkpoints = build_route_checkpoints(route["coordinates"])
        stations_on_route = find_stations_along_route(checkpoints)

        if not stations_on_route:
            return Response(
                {"error": "No fuel stations found along this route."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # ── Greedy fuel optimization ────────────────────────────────────
        try:
            stops, total_cost = optimize_fuel_stops(
                stations_on_route, total_mi
            )
        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # ── Build and cache response ────────────────────────────────────
        data = _build_response(
            route["coordinates"], stops, total_mi, total_h, total_cost
        )

        try:
            RouteCache.objects.update_or_create(
                cache_key=key,
                defaults={
                    "start_input": start,
                    "end_input": end,
                    "response_data": data,
                }
            )
        except Exception as e:
            logger.warning("Failed to cache route response: %s", e)

        return Response(data)
