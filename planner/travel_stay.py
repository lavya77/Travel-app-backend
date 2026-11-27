"""Suggested travel and stay options, and the sightseeing window they leave.

Prices here are typical-fare estimates, never live quotes. Every suggestion is
saved with is_estimate=True so the UI can label it and the user can replace it.
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, radians, sin, cos, asin, sqrt
from urllib.parse import quote_plus

from django.core.cache import cache
from django.utils import timezone

from destinations.models import Hotel
from integrations.geocoding import get_coordinates
from trips.models import TransportOption, Accommodation

logger = logging.getLogger(__name__)

# Below this straight-line distance the trip is local: no intercity travel.
LOCAL_TRIP_KM = 40
ROAD_FACTOR = 1.3

ALLOWED_TYPES = {
    "self_vehicle": ["car"],
    "rented_vehicle": ["car"],
    "public_transport": ["flight", "train", "bus"],
    "any": ["flight", "train", "bus", "car"],
}

# Time the traveller needs after arriving (transfer + check-in) and before
# departing (transfer + check-in at the terminal).
ARRIVAL_BUFFER_MINUTES = {"flight": 90, "train": 60, "bus": 60, "car": 45}
DEPARTURE_BUFFER_MINUTES = {"flight": 180, "train": 90, "bus": 90, "car": 30}

# A day needs at least this much free time to hold a sightseeing stop.
MIN_SIGHTSEEING_WINDOW = 75

ROOM_PRICE_BY_CATEGORY = {
    "hostel": 900,       # per bed
    "guest_house": 1600,
    "motel": 2000,
    "apartment": 3200,
    "hotel": 3500,
    "chalet": 5000,
}
ACCOMMODATION_TYPE_BY_CATEGORY = {
    "hotel": "hotel",
    "hostel": "hostel",
    "guest_house": "guesthouse",
    "motel": "hotel",
    "apartment": "homestay",
    "chalet": "resort",
}
FALLBACK_STAYS = [
    ("Budget stay", "guesthouse", 1500),
    ("Mid-range hotel", "hotel", 3200),
    ("Comfort hotel", "hotel", 6000),
]


def km_between(lat_a, lon_a, lat_b, lon_b):
    lat_a, lon_a, lat_b, lon_b = map(
        radians, [float(lat_a), float(lon_a), float(lat_b), float(lon_b)]
    )
    h = (
        sin((lat_b - lat_a) / 2) ** 2
        + cos(lat_a) * cos(lat_b) * sin((lon_b - lon_a) / 2) ** 2
    )
    return 6371 * 2 * asin(sqrt(h))


def money(value):
    return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def source_coordinates(city):
    key = f"geo:{city.strip().lower()}"
    cached = cache.get(key)
    if cached:
        return cached
    try:
        result = get_coordinates(city, timeout=8)
    except Exception as exc:  # network or rate-limit failure
        logger.warning("Geocoding %s failed: %s", city, exc)
        return None
    if result:
        coords = (result["latitude"], result["longitude"])
        cache.set(key, coords, 60 * 60 * 24 * 30)
        return coords
    return None


def booking_link(mode, origin, destination, day):
    if mode == "flight":
        return (
            "https://www.google.com/travel/flights?q="
            + quote_plus(f"Flights from {origin} to {destination} on {day.isoformat()}")
        )
    if mode == "train":
        return "https://www.irctc.co.in/nget/train-search"
    if mode == "bus":
        return "https://www.redbus.in/"
    return (
        "https://www.google.com/maps/dir/?api=1&travelmode=driving"
        f"&origin={quote_plus(origin)}&destination={quote_plus(destination)}"
    )


def mode_estimates(straight_km, people, travel_mode):
    """Typical duration (minutes) and total cost (INR) per feasible mode."""
    road_km = straight_km * ROAD_FACTOR
    people = max(int(people or 1), 1)
    estimates = []
    for mode in ALLOWED_TYPES.get(travel_mode, ALLOWED_TYPES["any"]):
        if mode == "flight" and straight_km >= 300:
            minutes = 45 + straight_km / 700 * 60
            cost = (2500 + 4.5 * straight_km) * people
            label = "Typical economy fare"
        elif mode == "train" and road_km <= 2500:
            minutes = road_km / 55 * 60
            cost = max(300, 1.6 * road_km) * people
            label = "Typical 3A sleeper fare"
        elif mode == "bus" and road_km <= 1000:
            minutes = road_km / 45 * 60
            cost = max(200, 1.8 * road_km) * people
            label = "Typical AC sleeper bus fare"
        elif mode == "car" and road_km <= (
            2500 if travel_mode in ("self_vehicle", "rented_vehicle") else 900
        ):
            minutes = road_km / 50 * 60
            # About 600 km of driving a day; stop overnight (8h) in between.
            halts = int((road_km - 1) // 600)
            minutes += halts * 8 * 60
            per_km = 14 if travel_mode == "rented_vehicle" else 8
            cars = ceil(people / 5)
            cost = per_km * road_km * cars
            label = (
                "Rental + fuel + tolls" if travel_mode == "rented_vehicle"
                else "Fuel + tolls"
            )
            if halts:
                label += f" · {halts} overnight halt{'s' if halts > 1 else ''}"
        else:
            continue
        estimates.append({
            "mode": mode,
            "minutes": int(round(minutes / 5) * 5),
            "cost": money(cost),
            "label": label,
        })
    return estimates


def outbound_times(mode, minutes, start_date):
    tz = timezone.get_current_timezone()
    if mode == "flight" or minutes <= 6 * 60:
        depart = datetime.combine(start_date, time(7, 0))
    else:
        # Long ground journeys run overnight and arrive early on day 1.
        depart = datetime.combine(start_date, time(7, 0)) - timedelta(minutes=minutes)
    depart = timezone.make_aware(depart, tz)
    return depart, depart + timedelta(minutes=minutes)


def return_times(mode, minutes, end_date):
    tz = timezone.get_current_timezone()
    if mode == "flight":
        depart = datetime.combine(end_date, time(19, 0))
    elif minutes <= 6 * 60:
        depart = datetime.combine(end_date, time(17, 0))
    else:
        depart = datetime.combine(end_date, time(20, 0))
    depart = timezone.make_aware(depart, tz)
    return depart, depart + timedelta(minutes=minutes)


def ensure_transport_suggestions(trip, destination_coords):
    """Keep 2-3 estimated options per direction that match the trip as it is now.

    Returns a warning string or None.
    """
    estimates = TransportOption.objects.filter(trip=trip, is_estimate=True)
    stale_ids = [
        option.id for option in estimates
        if (
            option.origin != (trip.source if option.direction == "outbound" else trip.destination)
            or option.destination != (trip.destination if option.direction == "outbound" else trip.source)
            or timezone.localtime(option.arrival_time if option.direction == "outbound" else option.departure_time).date()
            != (trip.start_date if option.direction == "outbound" else trip.end_date)
            or option.transport_type not in ALLOWED_TYPES.get(trip.travel_mode, ALLOWED_TYPES["any"])
        )
    ]
    if stale_ids:
        TransportOption.objects.filter(id__in=stale_ids).delete()

    have = set(
        TransportOption.objects
        .filter(trip=trip, is_estimate=True)
        .values_list("direction", flat=True)
    )
    if {"outbound", "return"} <= have:
        return None

    if not destination_coords:
        return "Travel options could not be estimated: no places are saved for the destination yet."
    origin_coords = source_coordinates(trip.source)
    if not origin_coords:
        return f"Travel options could not be estimated: {trip.source} was not found on the map. Add your own option."

    straight_km = km_between(*origin_coords, *destination_coords)
    if straight_km < LOCAL_TRIP_KM:
        return None

    options = mode_estimates(straight_km, trip.number_of_people, trip.travel_mode)
    # Keep the three best by a simple cost/time balance.
    options.sort(key=lambda o: float(o["cost"]) / 1000 + o["minutes"] / 120)
    options = options[:3]
    if not options:
        return "No travel mode fits this distance and travel preference. Add your own option."

    created = []
    for direction in ("outbound", "return"):
        if direction in have:
            continue
        origin, destination = (
            (trip.source, trip.destination) if direction == "outbound"
            else (trip.destination, trip.source)
        )
        for option in options:
            depart, arrive = (
                outbound_times(option["mode"], option["minutes"], trip.start_date)
                if direction == "outbound"
                else return_times(option["mode"], option["minutes"], trip.end_date)
            )
            created.append(TransportOption(
                trip=trip,
                direction=direction,
                transport_type=option["mode"],
                origin=origin,
                destination=destination,
                departure_time=depart,
                arrival_time=arrive,
                estimated_cost=option["cost"],
                provider=f"{option['label']} (estimate, ~{round(straight_km * (1 if option['mode'] == 'flight' else ROAD_FACTOR))} km)",
                booking_url=booking_link(option["mode"], origin, destination, depart.date()),
                is_estimate=True,
            ))
    TransportOption.objects.bulk_create(created)
    return None


def stay_price(category, people):
    people = max(int(people or 1), 1)
    per_unit = ROOM_PRICE_BY_CATEGORY.get(category, ROOM_PRICE_BY_CATEGORY["hotel"])
    units = people if category == "hostel" else ceil(people / 2)
    return money(per_unit * units)


def ensure_hotel_suggestions(trip, destination, centroid):
    """Add up to three estimated stays near the sightseeing centre, once per trip."""
    if (trip.end_date - trip.start_date).days <= 0:
        return
    if Accommodation.objects.filter(trip=trip, is_estimate=True).exists():
        return

    picks = []
    hotels = list(Hotel.objects.filter(destination=destination)) if destination else []
    if hotels and centroid:
        hotels.sort(key=lambda h: km_between(h.latitude, h.longitude, *centroid))
        nearby = hotels[:40]
        tiers = [
            {"hostel", "guest_house"},
            {"hotel", "motel", "apartment"},
            {"hotel", "chalet", "apartment"},
        ]
        for tier in tiers:
            hotel = next(
                (h for h in nearby if h.category in tier and h not in picks),
                None,
            )
            if hotel:
                picks.append(hotel)
        for hotel in nearby:
            if len(picks) >= 3:
                break
            if hotel not in picks:
                picks.append(hotel)

    rows = []
    for hotel in picks[:3]:
        address = ", ".join(p for p in [hotel.street, hotel.city] if p)
        rows.append(Accommodation(
            trip=trip,
            hotel=hotel,
            name=hotel.name,
            accommodation_type=ACCOMMODATION_TYPE_BY_CATEGORY.get(hotel.category, "hotel"),
            location=address or trip.destination,
            price_per_night=stay_price(hotel.category, trip.number_of_people),
            booking_url=(
                hotel.website if (hotel.website or "").startswith(("http://", "https://"))
                else "https://www.google.com/travel/hotels?q=" + quote_plus(f"{hotel.name} {trip.destination}")
            )[:200],
            is_estimate=True,
        ))
    for name, kind, price in FALLBACK_STAYS[len(rows):]:
        rooms = ceil(max(trip.number_of_people, 1) / 2)
        rows.append(Accommodation(
            trip=trip,
            name=f"{name} in {trip.destination}",
            accommodation_type=kind,
            location=trip.destination,
            price_per_night=money(price * rooms),
            booking_url="https://www.google.com/travel/hotels?q=" + quote_plus(f"{name} {trip.destination}"),
            is_estimate=True,
        ))
    Accommodation.objects.bulk_create(rows)


def day_windows(trip, outbound=None, return_option=None):
    """Sightseeing (start, end) per trip day, or None for a day with no time.

    outbound / return_option are dicts with transport_type and
    arrival_time / departure_time (aware datetimes).
    """
    from planner.services import DAY_START_TIME, DAY_CLOSING_TIME

    total_days = max((trip.end_date - trip.start_date).days + 1, 1)
    windows = []
    for index in range(total_days):
        day = trip.start_date + timedelta(days=index)
        start = datetime.combine(day, DAY_START_TIME)
        end = datetime.combine(day, DAY_CLOSING_TIME)

        if outbound:
            arrive = timezone.localtime(outbound["arrival_time"]).replace(tzinfo=None)
            ready = arrive + timedelta(
                minutes=ARRIVAL_BUFFER_MINUTES.get(outbound["transport_type"], 60)
            )
            if ready.date() > day:
                windows.append(None)
                continue
            if ready.date() == day:
                start = max(start, round_up_quarter(ready))

        if return_option:
            depart = timezone.localtime(return_option["departure_time"]).replace(tzinfo=None)
            leave = depart - timedelta(
                minutes=DEPARTURE_BUFFER_MINUTES.get(return_option["transport_type"], 90)
            )
            if leave.date() < day:
                windows.append(None)
                continue
            if leave.date() == day:
                end = min(end, leave)

        if (end - start).total_seconds() / 60 < MIN_SIGHTSEEING_WINDOW:
            windows.append(None)
        else:
            windows.append((start.time(), end.time()))
    return windows


def round_up_quarter(moment):
    extra = (-moment.minute) % 15
    moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=extra)
    return moment
