import math
from dataclasses import dataclass
from typing import List
from apps.stations.models import GasStation

EARTH_RADIUS_MILES = 3958.8
MILES_PER_DEG_LAT = 69.0
SAMPLE_INTERVAL_MI = 15.0    # checkpoint every ~15 miles along route
CORRIDOR_RADIUS_MI = 10.0    # station must be within 10 miles of route


@dataclass
class StationOnRoute:
    station: GasStation
    route_pos_miles: float     # cumulative miles from start at snap point


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """
    Calculate the great-circle distance in miles between two points
    on Earth using the Haversine formula.
    """
    R = EARTH_RADIUS_MILES
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def build_route_checkpoints(
    ors_coordinates: list,
    interval: float = SAMPLE_INTERVAL_MI
) -> list[tuple[float, float, float]]:
    """
    Sample the dense ORS polyline into evenly-spaced checkpoints.
    ORS input is [[lon, lat], ...]. Output is [(lat, lon, cum_miles), ...].
    Always includes the first and last coordinate.
    """
    if not ors_coordinates:
        return []

    checkpoints = []
    cum_miles = 0.0
    last_sample_mi = 0.0
    prev_lon, prev_lat = ors_coordinates[0]
    checkpoints.append((prev_lat, prev_lon, 0.0))

    for coord in ors_coordinates[1:]:
        lon, lat = coord
        step_mi = haversine_miles(prev_lat, prev_lon, lat, lon)
        cum_miles += step_mi

        if cum_miles - last_sample_mi >= interval:
            checkpoints.append((lat, lon, cum_miles))
            last_sample_mi = cum_miles

        prev_lat, prev_lon = lat, lon

    # Always include the final point
    last_lon, last_lat = ors_coordinates[-1]
    if not checkpoints or checkpoints[-1][2] != cum_miles:
        checkpoints.append((last_lat, last_lon, cum_miles))
    return checkpoints


def find_stations_along_route(
    checkpoints: list[tuple[float, float, float]],
    corridor_mi: float = CORRIDOR_RADIUS_MI
) -> list[StationOnRoute]:
    """
    Two-pass spatial filter:
      Pass 1 — Bounding box DB query (indexed, fast pre-filter)
      Pass 2 — Haversine distance per candidate (exact corridor check)

    For each station, finds the checkpoint of minimum distance; if that
    distance <= corridor_mi, the station is snapped to that checkpoint's
    cumulative mileage and included in the result.

    Returns stations sorted ascending by route_pos_miles.
    """
    if not checkpoints:
        return []

    lats = [c[0] for c in checkpoints]
    lons = [c[1] for c in checkpoints]
    avg_lat = sum(lats) / len(lats)

    # Compute degree-padding that matches the corridor radius.
    # Longitude degrees vary with latitude, so we scale accordingly.
    lat_pad = corridor_mi / MILES_PER_DEG_LAT
    lon_pad = corridor_mi / (MILES_PER_DEG_LAT * math.cos(math.radians(avg_lat)))

    lat_min, lat_max = min(lats) - lat_pad, max(lats) + lat_pad
    lon_min, lon_max = min(lons) - lon_pad, max(lons) + lon_pad

    # Pass 1: DB bounding box (uses lat/lon DB indexes)
    candidates = list(
        GasStation.objects.filter(
            geocoded=True,
            lat__gte=lat_min, lat__lte=lat_max,
            lon__gte=lon_min, lon__lte=lon_max,
        ).values(
            "id", "opis_id", "name", "city", "state",
            "lat", "lon", "retail_price"
        )
    )

    # Pass 2: Haversine proximity — snap each station to nearest checkpoint
    result_map: dict[int, StationOnRoute] = {}

    for s in candidates:
        best_dist = float("inf")
        best_snap = None

        for c_lat, c_lon, cum_mi in checkpoints:
            d = haversine_miles(c_lat, c_lon, s["lat"], s["lon"])
            if d < best_dist:
                best_dist = d
                best_snap = cum_mi

        if best_dist <= corridor_mi:
            obj = GasStation(
                id=s["id"], opis_id=s["opis_id"], name=s["name"],
                city=s["city"], state=s["state"],
                lat=s["lat"], lon=s["lon"],
                retail_price=s["retail_price"]
            )
            result_map[s["opis_id"]] = StationOnRoute(
                station=obj, route_pos_miles=best_snap
            )

    return sorted(result_map.values(), key=lambda x: x.route_pos_miles)
