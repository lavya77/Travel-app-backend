
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from itertools import product
from math import radians, sin, cos, asin, sqrt

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from destinations.models import Place, Hotel
from integrations.flights import search_flights
from planner.ai import build_ai_schedule
from planner.models import Plan, ItineraryItem
from trips.models import TransportOption, Accommodation

logger = logging.getLogger(__name__)

# ---------------- CONFIGURATION ----------------

AIRPORT_CODES = {
    "delhi": "DEL",
    "new delhi": "DEL",
    "mumbai": "BOM",
    "bengaluru": "BLR",
    "bangalore": "BLR",
    "hyderabad": "HYD",
    "chennai": "MAA",
    "kolkata": "CCU",
    "pune": "PNQ",
    "jaipur": "JAI",
    "shimla": "SLV",
    "manali": "KUU",
    "kullu": "KUU",
    "goa": "GOI",
    "kochi": "COK",
    "ahmedabad": "AMD",
    "lucknow": "LKO",
    "varanasi": "VNS",
    "indore": "IDR",
    "bhopal": "BHO",
    "amritsar": "ATQ",
    "chandigarh": "IXC",
    "srinagar": "SXR",
    "dehradun": "DED",
    "patna": "PAT",
    "guwahati": "GAU",
    "nagpur": "NAG",
    "udaipur": "UDR",
    "jodhpur": "JDH",
}

MEAL_CATEGORIES = {"restaurant", "cafe"}

NON_VISIT_CATEGORIES = {
    "hotel",
    "hostel",
    "guest_house",
    "guesthouse",
    "motel",
    "apartment",
    "information",
    "fuel",
    "parking",
    "toilets",
    "bus_station",
    "railway_station",
}

PLACE_PRIORITY = {
    "attraction": 1,
    "viewpoint": 2,
    "trek": 3,
    "shopping": 4,
    "other": 5,
}

# ---------------- FLIGHT HELPERS ----------------

def resolve_airport_code(location):
    value = str(location).strip()
    if len(value) == 3 and value.isalpha():
        return value.upper()
    city = value.split(",")[0].strip().lower()
    return AIRPORT_CODES.get(city)


def parse_flight_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def get_flight_candidates(trip):
    if trip.travel_mode not in ["any", "public_transport"]:
        return [], None

    origin_code = resolve_airport_code(trip.source)
    destination_code = resolve_airport_code(trip.destination)

    if not origin_code or not destination_code:
        return [], "Flight search skipped: airport mapping unavailable."

    if origin_code == destination_code:
        return [], "Flight search skipped: both airport codes are identical."

    if not 1 <= trip.number_of_people <= 9:
        return [], "Flight search skipped: passenger count must be 1 to 9."

    try:
        result = search_flights(
            origin=origin_code,
            destination=destination_code,
            date_from=trip.start_date.isoformat(),
            adults=trip.number_of_people,
            currency="INR",
            limit=20,
            sort="price",
        )

        candidates = []

        for offer in result.get("offers", []):
            currency = (offer.get("currency") or "").upper()
            if currency != "INR":
                continue

            outbound = offer.get("outbound") or {}
            segments = outbound.get("segments") or []

            if not segments:
                continue

            first_segment = segments[0]
            last_segment = segments[-1]

            departure = parse_flight_datetime(
                first_segment.get("departure")
            )
            arrival = parse_flight_datetime(
                last_segment.get("arrival")
            )

            if not departure or not arrival or arrival <= departure:
                continue

            try:
                price = Decimal(str(offer.get("price")))
            except (InvalidOperation, TypeError, ValueError):
                continue

            if price <= 0:
                continue

            duration = (
                outbound.get("duration_seconds")
                or outbound.get("total_duration_seconds")
                or (arrival - departure).total_seconds()
            )

            airline = (
                first_segment.get("airline_name")
                or offer.get("owner_airline")
                or "Airline"
            )

            candidates.append({
                "kind": "provider",
                "transport_type": "flight",
                "origin": first_segment.get("origin") or origin_code,
                "destination": (
                    last_segment.get("destination") or destination_code
                ),
                "departure_time": departure,
                "arrival_time": arrival,
                "estimated_cost": price,
                "duration_seconds": float(duration),
                "provider": airline,
                "booking_url": offer.get("booking_url") or "",
                "flight_number": first_segment.get("flight_no", ""),
                "stops": outbound.get("stopovers", 0),
                "currency": currency,
            })

        if not candidates:
            return [], "No usable INR flight offers were returned."

        return candidates, None

    except Exception:
        logger.exception("Flight search failed during plan generation")
        return [], "Flight provider is temporarily unavailable."


# ---------------- SAVED TRANSPORT ----------------

def get_saved_transport_candidates(trip):
    allowed_types = {
        "self_vehicle": ["car", "bike"],
        "rented_vehicle": ["car"],
        "public_transport": ["flight"],
        "any": ["flight", "car", "bike"],
    }

    selected_types = allowed_types.get(
        trip.travel_mode,
        allowed_types["any"]
    )

    options = TransportOption.objects.filter(
        trip=trip,
        transport_type__in=selected_types
    )

    candidates = []

    for option in options:
        if option.departure_time.date() != trip.start_date:
            continue

        duration = (
            option.arrival_time - option.departure_time
        ).total_seconds()

        if duration < 0 or option.estimated_cost <= 0:
            continue

        candidates.append({
            "kind": "saved",
            "transport_type": option.transport_type,
            "origin": option.origin,
            "destination": option.destination,
            "departure_time": option.departure_time,
            "arrival_time": option.arrival_time,
            "estimated_cost": option.estimated_cost,
            "duration_seconds": duration,
            "provider": option.provider,
            "booking_url": option.booking_url,
            "model_id": option.id,
        })

    return candidates


def remove_duplicate_transport_candidates(candidates):
    unique_candidates = {}

    for item in candidates:
        key = (
            item["transport_type"],
            item["origin"],
            item["destination"],
            item["departure_time"].isoformat(),
            item["arrival_time"].isoformat(),
            str(item["estimated_cost"]),
        )

        if key not in unique_candidates:
            unique_candidates[key] = item

    return list(unique_candidates.values())


def save_selected_flight(trip, selected):
    if not selected or selected["kind"] != "provider":
        return selected

    option, created = TransportOption.objects.get_or_create(
        trip=trip,
        transport_type="flight",
        origin=selected["origin"],
        destination=selected["destination"],
        departure_time=selected["departure_time"],
        arrival_time=selected["arrival_time"],
        estimated_cost=selected["estimated_cost"],
        defaults={
            "provider": selected["provider"],
            "booking_url": selected["booking_url"],
        }
    )

    selected["model_id"] = option.id
    selected["kind"] = "saved"

    return selected


# ---------------- COST OPTIMIZATION ----------------

def get_priced_accommodation_candidates(trip):
    nights = (trip.end_date - trip.start_date).days

    if nights <= 0:
        return []

    return list(
        Accommodation.objects.filter(
            trip=trip,
            price_per_night__gt=0
        )
        .select_related("hotel", "hotel__destination")
    )


def calculate_attraction_cost(itinerary):
    return sum(
        (
            item["place"].estimated_cost
            for item in itinerary
            if item["place"].estimated_cost > 0
        ),
        Decimal("0.00")
    )


def choose_optimal_combination(
    transport_candidates,
    accommodation_candidates,
    nights,
    attraction_cost,
    budget,
):
    """
    Evaluate transport and accommodation together.

    Prefer combinations within budget. Among feasible combinations,
    choose the lowest known total, using journey duration as a tie-breaker.

    If no combination fits, return the cheapest available combination
    and indicate that the known cost exceeds the budget.

    Missing costs are never treated as free services.
    """

    transport_options = transport_candidates or [None]
    accommodation_options = accommodation_candidates or [None]

    combinations = []

    for transport, accommodation in product(
        transport_options,
        accommodation_options
    ):
        transport_cost = (
            transport["estimated_cost"]
            if transport else Decimal("0.00")
        )

        accommodation_cost = (
            accommodation.price_per_night * nights
            if accommodation else Decimal("0.00")
        )

        known_total = (
            transport_cost
            + accommodation_cost
            + attraction_cost
        )

        duration = (
            transport["duration_seconds"]
            if transport else float("inf")
        )

        combinations.append({
            "transport": transport,
            "accommodation": accommodation,
            "transport_cost": transport_cost,
            "accommodation_cost": accommodation_cost,
            "attraction_cost": attraction_cost,
            "known_total": known_total,
            "within_known_budget": known_total <= budget,
            "duration_seconds": duration,
        })

    if not combinations:
        return None, 0

    feasible = [
        item for item in combinations
        if item["within_known_budget"]
    ]

    # Prefer feasible combinations. Otherwise choose the cheapest.
    pool = feasible if feasible else combinations

    selected = min(
        pool,
        key=lambda item: (
            item["known_total"],
            item["duration_seconds"],
            (
                item["accommodation"].price_per_night
                if item["accommodation"]
                else Decimal("0.00")
            ),
        )
    )

    return selected, len(combinations)


def calculate_unknown_costs(
    selected_transport,
    selected_accommodation,
    itinerary,
    dining_recommendations,
    nights,
):
    unknown_components = []

    if not selected_transport:
        unknown_components.append("outbound transport")

    # Return flight / return transport is not implemented yet.
    unknown_components.append("return transport")

    if nights > 0 and not selected_accommodation:
        unknown_components.append("accommodation")

    # Local road journeys between attractions are approximations.
    unknown_components.append("local transport")

    # Restaurant discovery does not provide meal prices.
    unknown_components.append("meals")

    if any(
        item["place"].estimated_cost <= 0
        for item in itinerary
    ):
        unknown_components.append("attraction entry fees")

    return unknown_components


# ---------------- GEOGRAPHIC DISTANCE ----------------

def haversine_distance(place_a, place_b):
    lat1 = radians(float(place_a.latitude))
    lon1 = radians(float(place_a.longitude))
    lat2 = radians(float(place_b.latitude))
    lon2 = radians(float(place_b.longitude))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    value = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )

    return 6371 * 2 * asin(sqrt(value))


def place_priority(place):
    category = (place.category or "").lower()
    return PLACE_PRIORITY.get(category, 6)


# ---------------- DINING RECOMMENDATIONS ----------------

def get_dining_recommendations(places):
    dining_places = [
        place for place in places
        if (place.category or "").lower() in MEAL_CATEGORIES
    ]

    dining_places.sort(
        key=lambda place: (
            0 if place.category == "restaurant" else 1,
            place.name.lower()
        )
    )

    return [
        {
            "id": place.id,
            "name": place.name,
            "category": place.category,
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
            "estimated_cost": (
                str(place.estimated_cost)
                if place.estimated_cost > 0
                else None
            ),
            "price_available": place.estimated_cost > 0,
        }
        for place in dining_places
    ]


# ---------------- OTHER NEARBY PLACES ----------------

def get_other_place_recommendations(places):
    excluded_categories = (
        MEAL_CATEGORIES
        | set(PLACE_PRIORITY.keys())
        | NON_VISIT_CATEGORIES
    )

    return [
        {
            "id": place.id,
            "name": place.name,
            "category": place.category,
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
        }
        for place in places
        if (place.category or "").lower() not in excluded_categories
    ]


# ---------------- SIGHTSEEING ITINERARY ----------------

def build_daily_schedule(places, trip, ai_days=None):
    """Distribute sightseeing across trip days and group nearby places."""

    candidates = [
        place for place in places
        if (place.category or "").lower()
        not in MEAL_CATEGORIES | NON_VISIT_CATEGORIES
    ]

    if not candidates:
        return [], []

    total_days = max((trip.end_date - trip.start_date).days + 1, 1)
    seed_count = min(total_days, len(candidates))

    def visit_duration(place):
        return max(int(place.time_required or 60), 30)

    place_by_id = {place.id: place for place in candidates}

    if ai_days is not None:
        clusters = [
            {"places": [], "load": 0}
            for _ in range(total_days)
        ]
        ai_ranks = {}

        for day_number, place_ids in ai_days.items():
            cluster = clusters[day_number - 1]
            for rank, place_id in enumerate(place_ids):
                place = place_by_id[place_id]
                cluster["places"].append(place)
                cluster["load"] += visit_duration(place)
                ai_ranks[(day_number, place_id)] = rank
    else:
        # Seed each day with a geographically separated attraction.
        first_seed = min(
            candidates,
            key=lambda place: (place_priority(place), -visit_duration(place)),
        )

        seeds = [first_seed]
        remaining = [
            place for place in candidates
            if place.id != first_seed.id
        ]

        while len(seeds) < seed_count and remaining:
            next_seed = max(
                remaining,
                key=lambda place: (
                    min(haversine_distance(place, seed) for seed in seeds),
                    -place_priority(place),
                    visit_duration(place),
                ),
            )

            seeds.append(next_seed)
            remaining.remove(next_seed)

        # Balance the remaining workload while preferring nearby clusters.
        clusters = [
            {"places": [seed], "load": visit_duration(seed)}
            for seed in seeds
        ]

        remaining.sort(
            key=lambda place: (-visit_duration(place), place_priority(place))
        )

        for place in remaining:
            cluster = min(
                clusters,
                key=lambda item: (
                    item["load"]
                    + min(
                        haversine_distance(place, existing)
                        for existing in item["places"]
                    ) * 8
                ),
            )

            cluster["places"].append(place)
            cluster["load"] += visit_duration(place)
        ai_ranks = {}

    scheduled = []
    scheduled_ids = set()
    closing_time = time(17, 30)
    lunch_start_time = time(12, 30)
    lunch_end_time = time(13, 15)
    active_limit = 420

    for day_index, cluster in enumerate(clusters):
        day_number = day_index + 1
        day_date = trip.start_date + timedelta(days=day_index)
        current_time = datetime.combine(day_date, time(9, 0))
        previous_place = None
        active_minutes = 0
        lunch_taken = False
        order = 1
        day_remaining = list(cluster["places"])

        while day_remaining:
            feasible = []

            for candidate in day_remaining:
                if previous_place is None:
                    distance = 0.0
                    travel_minutes = 0
                else:
                    distance = haversine_distance(previous_place, candidate)

                    # Approximation only: straight-line distance at 25 km/hour.
                    travel_minutes = max(
                        5,
                        min(60, int((distance / 25) * 60))
                    )

                duration = visit_duration(candidate)
                start_time = current_time + timedelta(minutes=travel_minutes)
                candidate_lunch_taken = lunch_taken
                lunch_start = datetime.combine(day_date, lunch_start_time)
                lunch_end = datetime.combine(day_date, lunch_end_time)

                if not lunch_taken:
                    end_before_lunch = start_time + timedelta(minutes=duration)

                    if lunch_start <= start_time < lunch_end:
                        start_time = lunch_end
                        candidate_lunch_taken = True

                    elif start_time < lunch_start and end_before_lunch > lunch_start:
                        start_time = lunch_end
                        candidate_lunch_taken = True

                    elif start_time >= lunch_end:
                        candidate_lunch_taken = True

                end_time = start_time + timedelta(minutes=duration)

                if active_minutes + travel_minutes + duration > active_limit:
                    continue

                if end_time.time() > closing_time:
                    continue

                feasible.append({
                    "place": candidate,
                    "distance": distance,
                    "travel_minutes": travel_minutes,
                    "visit_minutes": duration,
                    "start": start_time,
                    "end": end_time,
                    "lunch_taken": candidate_lunch_taken,
                })

            if not feasible:
                break

            selected = min(
                feasible,
                key=lambda item: (
                    item["distance"]
                    + ai_ranks.get((day_number, item["place"].id), 0) * 0.5,
                    item["distance"],
                    place_priority(item["place"]),
                    item["place"].name.lower(),
                ),
            )

            place = selected["place"]

            scheduled.append({
                "day": day_number,
                "place": place,
                "start_time": selected["start"].time(),
                "end_time": selected["end"].time(),
                "order": order,
                "visit_minutes": selected["visit_minutes"],
                "travel_minutes_before": selected["travel_minutes"],
                "distance_before_km": round(selected["distance"], 2),
            })

            scheduled_ids.add(place.id)
            active_minutes += (
                selected["travel_minutes"] + selected["visit_minutes"]
            )
            current_time = selected["end"]
            previous_place = place
            lunch_taken = selected["lunch_taken"]
            day_remaining.remove(place)
            order += 1

    unscheduled = [
        place for place in candidates
        if place.id not in scheduled_ids
    ]

    return scheduled, unscheduled


# ---------------- FULL-DAY TIMELINE ----------------

def build_daily_timeline(itinerary, dining_recommendations, trip):
    """Add lunch, dinner, and explicit free-time blocks to each trip day."""

    def to_minutes(value):
        if isinstance(value, str):
            hours, minutes = map(int, value.split(":")[:2])
        else:
            hours, minutes = value.hour, value.minute

        return hours * 60 + minutes

    def format_time(minutes):
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def distance_to_venue(anchor, venue):
        if not anchor:
            return 0.0

        lat1 = radians(float(anchor["latitude"]))
        lon1 = radians(float(anchor["longitude"]))
        lat2 = radians(float(venue["latitude"]))
        lon2 = radians(float(venue["longitude"]))

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        value = (
            sin(dlat / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        )

        return 6371 * 2 * asin(sqrt(value))

    used_venues = set()

    def choose_venue(anchor):
        available = [
            venue for venue in dining_recommendations
            if venue.get("latitude")
            and venue.get("longitude")
            and venue["id"] not in used_venues
        ]

        if not available:
            available = [
                venue for venue in dining_recommendations
                if venue.get("latitude") and venue.get("longitude")
            ]

        if not available:
            return None

        selected = min(
            available,
            key=lambda venue: (
                distance_to_venue(anchor, venue),
                0 if venue.get("category") == "restaurant" else 1,
                venue.get("name", "").lower(),
            ),
        )

        used_venues.add(selected["id"])
        return selected

    daily_schedule = []
    total_days = (trip.end_date - trip.start_date).days + 1

    for day in range(1, total_days + 1):
        day_date = trip.start_date + timedelta(days=day - 1)

        activities = sorted(
            [item for item in itinerary if item["day"] == day],
            key=lambda item: to_minutes(item["start_time"]),
        )

        first = activities[0] if activities else None
        last = activities[-1] if activities else None

        first_anchor = (
            {"latitude": first["latitude"], "longitude": first["longitude"]}
            if first else None
        )

        last_anchor = (
            {"latitude": last["latitude"], "longitude": last["longitude"]}
            if last else None
        )

        lunch_venue = choose_venue(first_anchor)
        dinner_venue = choose_venue(last_anchor)
        events = []

        for item in activities:
            events.append({
                "type": "sightseeing",
                "title": item["place"],
                "start_time": item["start_time"],
                "end_time": item["end_time"],
                "place": {
                    "id": item["id"],
                    "name": item["place"],
                    "category": item["category"],
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                },
                "note": (
                    "Estimated sightseeing duration; "
                    "local travel is approximate."
                ),
            })

        events.extend([
            {
                "type": "meal",
                "title": "Lunch",
                "start_time": "12:30",
                "end_time": "13:15",
                "place": lunch_venue,
                "note": (
                    "Suggested saved restaurant or cafe. "
                    "Verify opening hours and prices."
                    if lunch_venue else
                    "Lunch break; no saved dining venue is available."
                ),
            },
            {
                "type": "meal",
                "title": "Dinner",
                "start_time": "19:00",
                "end_time": "20:00",
                "place": dinner_venue,
                "note": (
                    "Suggested saved restaurant or cafe. "
                    "Verify opening hours and prices."
                    if dinner_venue else
                    "Dinner break; no saved dining venue is available."
                ),
            },
        ])

        events.sort(
            key=lambda event: to_minutes(event["start_time"])
        )

        complete_day = []
        cursor = 9 * 60
        closing = 21 * 60

        for event in events:
            start = to_minutes(event["start_time"])
            end = to_minutes(event["end_time"])

            if start < cursor:
                # Avoid overlaps while preserving the activity duration.
                if event["type"] == "meal":
                    continue

                duration = end - start
                start = cursor
                end = start + duration
                event["start_time"] = format_time(start)
                event["end_time"] = format_time(end)

                if end <= start:
                    continue

            if start > cursor:
                complete_day.append({
                    "type": "free_time",
                    "title": "Free time / leisure",
                    "start_time": format_time(cursor),
                    "end_time": format_time(start),
                    "place": None,
                    "note": (
                        "Unscheduled flexible time; "
                        "choose an activity or relax."
                    ),
                })

            if end > closing:
                end = closing
                event["end_time"] = format_time(end)

            if end > start:
                complete_day.append(event)
                cursor = end

        if cursor < closing:
            complete_day.append({
                "type": "free_time",
                "title": "Free time / evening leisure",
                "start_time": format_time(cursor),
                "end_time": format_time(closing),
                "place": None,
                "note": "Flexible time for personal activities and relaxation.",
            })

        daily_schedule.append({
            "day": day,
            "date": day_date.isoformat(),
            "events": complete_day,
        })

    return daily_schedule


# ---------------- HOTEL RECOMMENDATIONS ----------------

def get_hotel_recommendations(destination_name):
    hotels = Hotel.objects.filter(
        destination__name__iexact=destination_name
    ).order_by("name")

    return [
        {
            "id": hotel.id,
            "name": hotel.name,
            "category": hotel.category,
            "latitude": str(hotel.latitude),
            "longitude": str(hotel.longitude),
            "street": hotel.street,
            "city": hotel.city,
            "postcode": hotel.postcode,
            "website": hotel.website,
            "phone": hotel.phone,
            "price_available": False,
        }
        for hotel in hotels
    ]


# ---------------- MAIN PLANNING ENGINE ----------------

@transaction.atomic
def generate_optimal_plan(trip, use_ai=False):
    warnings = []

    if trip.end_date < trip.start_date:
        raise ValueError("Trip end date cannot be before start date.")

    if trip.number_of_people < 1:
        raise ValueError("At least one traveller is required.")

    nights = (trip.end_date - trip.start_date).days

    # 1. Retrieve saved destination places.
    places = list(
        Place.objects.filter(
            destination__name__iexact=trip.destination
        ).order_by("name")
    )

    if not places:
        warnings.append("No saved places found for this destination.")

    # 2. Separate sightseeing, dining and other places.
    dining_recommendations = get_dining_recommendations(places)
    nearby_recommendations = get_other_place_recommendations(places)

    sightseeing_places = [
        place for place in places
        if (place.category or "").lower()
        not in MEAL_CATEGORIES | NON_VISIT_CATEGORIES
    ]

    if not sightseeing_places:
        warnings.append("No sightseeing places were found for this destination.")

    # 3. Optionally use AI for day assignments; local rules remain authoritative.
    ai_days = None
    ai_provider = None
    if use_ai and sightseeing_places:
        ai_days, ai_provider, ai_warning = build_ai_schedule(
            trip,
            sightseeing_places,
        )
        if ai_warning:
            warnings.append(ai_warning)

    planner_mode = "rules"
    if ai_days is not None:
        itinerary, unscheduled_places = build_daily_schedule(
            sightseeing_places,
            trip,
            ai_days=ai_days,
        )
        standard_itinerary, standard_unscheduled = build_daily_schedule(
            sightseeing_places,
            trip,
        )
        if len(unscheduled_places) > len(standard_unscheduled):
            itinerary = standard_itinerary
            unscheduled_places = standard_unscheduled
            warnings.append(
                "The AI day assignments did not fit the schedule as well as "
                "the standard planner, so the standard itinerary was used."
            )
        else:
            planner_mode = "ai"
    else:
        itinerary, unscheduled_places = build_daily_schedule(
            sightseeing_places,
            trip,
        )

    if unscheduled_places:
        warnings.append(
            f"{len(unscheduled_places)} sightseeing places "
            "could not fit into the available itinerary days."
        )

    attraction_cost = calculate_attraction_cost(itinerary)

    # 4. Collect transport options.
    live_flights, flight_warning = get_flight_candidates(trip)

    if flight_warning:
        warnings.append(flight_warning)

    saved_transport = get_saved_transport_candidates(trip)

    transport_candidates = remove_duplicate_transport_candidates(
        saved_transport + live_flights
    )

    if not transport_candidates:
        warnings.append(
            "No matching transport option found. "
            "Outbound transport cost is unknown."
        )

    # 5. Collect all accommodation options with known prices.
    accommodation_candidates = get_priced_accommodation_candidates(trip)

    if nights > 0 and not accommodation_candidates:
        warnings.append(
            "No priced accommodation option is saved for this trip. "
            "Hotel recommendations do not contain live prices."
        )

    # 6. Compare transport and accommodation together.
    combination, combinations_considered = choose_optimal_combination(
        transport_candidates=transport_candidates,
        accommodation_candidates=accommodation_candidates,
        nights=nights,
        attraction_cost=attraction_cost,
        budget=trip.estimated_budget,
    )

    selected_transport = (
        combination["transport"] if combination else None
    )

    selected_accommodation = (
        combination["accommodation"] if combination else None
    )

    if selected_transport and selected_transport["kind"] == "provider":
        selected_transport = save_selected_flight(
            trip,
            selected_transport
        )

    transport_cost = (
        selected_transport["estimated_cost"]
        if selected_transport
        else Decimal("0.00")
    )

    accommodation_cost = (
        selected_accommodation.price_per_night * nights
        if selected_accommodation
        else Decimal("0.00")
    )

    known_total = (
        transport_cost
        + accommodation_cost
        + attraction_cost
    )

    # 7. Calculate missing cost components.
    unknown_components = calculate_unknown_costs(
        selected_transport,
        selected_accommodation,
        itinerary,
        dining_recommendations,
        nights,
    )

    if not unknown_components:
        budget_status = (
            "within_budget"
            if known_total <= trip.estimated_budget
            else "over_budget"
        )
    elif known_total > trip.estimated_budget:
        budget_status = "known_costs_over_budget"
    else:
        budget_status = "incomplete"

    if budget_status == "known_costs_over_budget":
        warnings.append(
            "Known trip costs already exceed the user's budget. "
            "The cheapest available combination has been selected."
        )
    elif budget_status == "incomplete":
        warnings.append(
            "Known costs fit within the budget, but the complete trip "
            "cannot be confirmed within budget because some expenses "
            "are still unknown."
        )

    # 8. Calculate effort.
    total_minutes = sum(
        item["visit_minutes"] + item["travel_minutes_before"]
        for item in itinerary
    )

    estimated_effort = round(total_minutes / 60, 2)

    # 9. Save/update the single optimal plan.
    with transaction.atomic():
        plan = (
            Plan.objects.filter(
                trip=trip,
                plan_type="optimal"
            )
            .order_by("-created_at")
            .first()
        )

        if plan:
            plan.itinerary_items.all().delete()
            plan.estimated_cost = known_total
            plan.estimated_effort = estimated_effort
            plan.save(
                update_fields=[
                    "estimated_cost",
                    "estimated_effort"
                ]
            )
        else:
            plan = Plan.objects.create(
                trip=trip,
                plan_type="optimal",
                estimated_cost=known_total,
                estimated_effort=estimated_effort,
            )

        saved_itinerary = []

        for item in itinerary:
            itinerary_item = ItineraryItem.objects.create(
                plan=plan,
                day=item["day"],
                place=item["place"],
                start_time=item["start_time"],
                end_time=item["end_time"],
                order=item["order"],
            )

            saved_itinerary.append({
                "id": itinerary_item.id,
                "day": item["day"],
                "place": item["place"].name,
                "category": item["place"].category,
                "start_time": item["start_time"].strftime("%H:%M"),
                "end_time": item["end_time"].strftime("%H:%M"),
                "order": item["order"],
                "latitude": str(item["place"].latitude),
                "longitude": str(item["place"].longitude),
                "visit_minutes": item["visit_minutes"],
                "travel_minutes_before": item["travel_minutes_before"],
                "distance_before_km": item["distance_before_km"],
            })

    # 10. Format selected transport.
    transport_summary = None

    if selected_transport:
        transport_summary = {
            "id": selected_transport.get("model_id"),
            "type": selected_transport["transport_type"],
            "origin": selected_transport["origin"],
            "destination": selected_transport["destination"],
            "departure_time": selected_transport["departure_time"].isoformat(),
            "arrival_time": selected_transport["arrival_time"].isoformat(),
            "duration_seconds": selected_transport["duration_seconds"],
            "estimated_cost": str(selected_transport["estimated_cost"]),
            "currency": "INR",
            "provider": selected_transport["provider"],
            "booking_url": selected_transport["booking_url"],
        }

    # 11. Format selected accommodation.
    accommodation_summary = None

    if selected_accommodation:
        accommodation_summary = {
            "id": selected_accommodation.id,
            "hotel_id": selected_accommodation.hotel_id,
            "name": selected_accommodation.name,
            "type": selected_accommodation.accommodation_type,
            "price_per_night": str(
                selected_accommodation.price_per_night
            ),
            "nights": nights,
            "estimated_total": str(accommodation_cost),
            "booking_url": selected_accommodation.booking_url,
            "price_source": "user_entered_unverified",
            "price_warning": (
                "Verify this nightly price against an actual hotel quote."
            ),
        }

    # 12. Format full cost breakdown.
    cost_breakdown = {
        "currency": "INR",
        "budget": str(trip.estimated_budget),
        "known_total": str(known_total),
        "transport": str(transport_cost),
        "accommodation": str(accommodation_cost),
        "attractions": str(attraction_cost),
        "remaining_known_budget": str(
            trip.estimated_budget - known_total
        ),
        "budget_status": budget_status,
        "within_known_budget": known_total <= trip.estimated_budget,
        "unknown_components": unknown_components,
        "combination_selection": (
            "Lowest known-cost transport and accommodation "
            "combination within budget. Shorter transport duration "
            "is used as a tie-breaker. If none fits, the cheapest "
            "available known-cost combination is returned."
        ),
        "combinations_considered": combinations_considered,
        "note": (
            "This is a partial trip estimate. Return transport, "
            "local transport, meals and unpriced attraction fees "
            "are not included. Accommodation prices are user-entered "
            "and must be verified."
        ),
    }

    if not selected_accommodation and nights > 0:
        cost_breakdown["accommodation"] = None

    if not selected_transport:
        cost_breakdown["transport"] = None

    # 13. Add transparent warnings.
    if selected_accommodation:
        warnings.append(
            "Accommodation price is user-entered and unverified. "
            "Confirm the actual hotel quotation before relying on it."
        )

    if not selected_transport:
        warnings.append("Outbound transport has no selected priced option.")

    if nights > 0:
        warnings.append(
            "Accommodation cost is calculated per night multiplied "
            "by trip nights. Room count and occupancy are not modeled."
        )

    return {
        "message": "Optimal travel plan generated successfully.",
        "plan_id": plan.id,
        "strategy": {
            "name": "Budget-aware optimal combination",
            "selection_method": (
                "Lowest known total within budget, with transport "
                "duration as a tie-breaker."
            ),
            "combinations_considered": combinations_considered,
        },
        "planner": {
            "mode": planner_mode,
            "provider": ai_provider if planner_mode == "ai" else None,
        },
        "trip": {
            "source": trip.source,
            "destination": trip.destination,
            "start_date": trip.start_date.isoformat(),
            "end_date": trip.end_date.isoformat(),
            "number_of_people": trip.number_of_people,
            "nights": nights,
            "budget": str(trip.estimated_budget),
        },
        "transport": transport_summary,
        "accommodation": accommodation_summary,
        "hotel_recommendations": get_hotel_recommendations(
            trip.destination
        ),
        "dining_recommendations": dining_recommendations,
        "nearby_recommendations": nearby_recommendations,
        "estimated_cost": cost_breakdown,
        "itinerary": saved_itinerary,
        "daily_schedule": build_daily_timeline(
            saved_itinerary,
            dining_recommendations,
            trip,
        ),
        "unscheduled_places": [
            place.name for place in unscheduled_places
        ],
        "warnings": warnings,
        "map_data_source": "OpenStreetMap",
        "map_attribution": "© OpenStreetMap contributors",
    }
