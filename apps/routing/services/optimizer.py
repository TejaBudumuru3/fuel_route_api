from dataclasses import dataclass
from typing import List
from .corridor import StationOnRoute

TANK_CAPACITY_GALLONS = 50.0
FUEL_CONSUMPTION_GPM = 0.1      # gallons per mile (10 MPG)
MAX_RANGE_MILES = 500.0
MIN_PURCHASE_GALLONS = 2.0      # skip stops requiring less than this


@dataclass
class FuelStop:
    stop_number: int
    name: str
    city: str
    state: str
    lat: float
    lon: float
    price_per_gallon: float
    gallons_purchased: float
    cost_at_stop: float
    miles_from_start: float
    tank_after_stop: float


def optimize_fuel_stops(
    stations: List[StationOnRoute],
    total_route_miles: float,
    start_tank: float = TANK_CAPACITY_GALLONS
) -> tuple[List[FuelStop], float]:
    """
    Greedy look-ahead fuel optimizer.

    Strategy:
    - At each step, decide whether to stop based on fuel level and prices.
    - If tank has enough fuel to skip to a cheaper station, skip this one.
    - If stopping, buy enough to reach the next cheaper station (or fill up).
    - Minimum purchase threshold avoids trivial 1-2 gallon micro-stops.

    Constants:
    - Vehicle starts with a full tank (50 gal)
    - Max range: 500 miles (50 gal * 10 MPG)
    - Fuel consumption: 0.1 gal/mile (10 MPG)

    Returns:
        (list_of_fuel_stops, total_cost)
    """
    tank = float(start_tank)
    current_pos = 0.0
    total_cost = 0.0
    stops: List[FuelStop] = []
    visited_positions = set()  # prevent infinite loops

    while current_pos < total_route_miles:
        remaining_range = tank * 10.0   # miles remaining in current tank
        max_reach = current_pos + remaining_range

        # Can we reach the destination without stopping?
        if max_reach >= total_route_miles:
            break

        # Find all reachable stations from current position
        reachable = [
            s for s in stations
            if current_pos < s.route_pos_miles <= max_reach
        ]

        if not reachable:
            raise ValueError(
                f"No reachable station at mile {current_pos:.1f}. "
                f"Route infeasible."
            )

        # Pick the cheapest reachable station
        cheapest = min(
            reachable,
            key=lambda s: float(s.station.retail_price)
        )

        # Guard against revisiting the same position (infinite loop safety)
        pos_key = round(cheapest.route_pos_miles, 1)
        if pos_key in visited_positions:
            # Force a fill-up and move past
            cheapest = max(reachable, key=lambda s: s.route_pos_miles)
            pos_key = round(cheapest.route_pos_miles, 1)
        visited_positions.add(pos_key)

        # Drive to that station
        miles_driven = cheapest.route_pos_miles - current_pos
        tank -= miles_driven * FUEL_CONSUMPTION_GPM
        current_pos = cheapest.route_pos_miles

        # Look-ahead: is there a cheaper station within 500 miles?
        ahead = [
            s for s in stations
            if s.route_pos_miles > current_pos
            and s.route_pos_miles <= current_pos + MAX_RANGE_MILES
        ]
        cheaper_ahead = [
            s for s in ahead
            if float(s.station.retail_price) < float(cheapest.station.retail_price)
        ]

        if cheaper_ahead:
            nearest = min(cheaper_ahead, key=lambda s: s.route_pos_miles)
            miles_to_cheaper = nearest.route_pos_miles - current_pos
            fuel_to_reach = miles_to_cheaper * FUEL_CONSUMPTION_GPM

            # If we already have enough fuel to reach the cheaper station,
            # skip this stop entirely — no point buying expensive fuel here
            if tank >= fuel_to_reach * 1.05:
                continue

            # Buy just enough to reach the nearest cheaper station (+5% buffer)
            needed = fuel_to_reach * 1.05
            to_buy = max(0.0, needed - tank)
            to_buy = min(to_buy, TANK_CAPACITY_GALLONS - tank)
        else:
            # No cheaper station ahead — fill to max
            miles_to_destination = total_route_miles - current_pos
            gallons_to_destination = miles_to_destination * FUEL_CONSUMPTION_GPM
            
            # Buy enough to reach the end, but don't overfill the physical tank
            to_buy = max(0.0, gallons_to_destination - tank)
            to_buy = min(to_buy, TANK_CAPACITY_GALLONS - tank)

        # Skip trivially small purchases (avoid micro-stops)
        to_buy = max(0.0, round(to_buy, 4))
        if to_buy < MIN_PURCHASE_GALLONS:
            # But if tank is critically low and we MUST buy, buy anyway
            miles_to_next_stop = float("inf")
            for s in stations:
                if s.route_pos_miles > current_pos:
                    miles_to_next_stop = s.route_pos_miles - current_pos
                    break
            remaining_after_drive = tank - (miles_to_next_stop * FUEL_CONSUMPTION_GPM)
            if remaining_after_drive > 5.0:  # >50 miles of buffer
                continue  # Safe to skip
            # Otherwise, buy at least enough to be safe
            to_buy = max(to_buy, min(MIN_PURCHASE_GALLONS, TANK_CAPACITY_GALLONS - tank))

        if to_buy < 0.001:
            continue

        cost = to_buy * float(cheapest.station.retail_price)
        tank += to_buy
        total_cost += cost

        stops.append(FuelStop(
            stop_number=len(stops) + 1,
            name=cheapest.station.name,
            city=cheapest.station.city,
            state=cheapest.station.state,
            lat=cheapest.station.lat,
            lon=cheapest.station.lon,
            price_per_gallon=round(float(cheapest.station.retail_price), 5),
            gallons_purchased=round(to_buy, 2),
            cost_at_stop=round(cost, 2),
            miles_from_start=round(current_pos, 1),
            tank_after_stop=round(tank, 2),
        ))

    return stops, round(total_cost, 2)
