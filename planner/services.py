
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from itertools import product
from math import ceil, radians, sin, cos, asin, sqrt

from django.db import transaction
from django.utils import timezone

from destinations.models import Place, Hotel
from integrations.save_places import find_destination_record
from planner.ai import build_ai_schedule
from planner.models import Plan, ItineraryItem
from planner.travel_stay import (
    day_windows,
    ensure_hotel_suggestions,
    ensure_transport_suggestions,
)
from trips.models import TransportOption, Accommodation

logger = logging.getLogger(__name__)

# ---------------- CONFIGURATION ----------------

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
    "museum": 2,
    "historic": 2,
    "zoo": 2,
    "gallery": 3,
    "theme_park": 3,
    "aquarium": 3,
    "place_of_worship": 3,
    "trek": 3,
    "park": 4,
    "shopping": 5,
    "other": 5,
}

# Visits a day should hold before the planner starts the next day: the
# 09:00-17:30 window less lunch and travel between stops.
DAY_TARGET_VISIT_MINUTES = 330
# Most stops a day can take (30-minute minimum visits); caps the pool of
# places considered for a trip so big cities stay fast.
MAX_STOPS_PER_DAY = 7

DAY_START_TIME = time(9, 0)
DAY_CLOSING_TIME = time(17, 30)
LUNCH_START_TIME = time(12, 30)
LUNCH_END_TIME = time(13, 15)
ACTIVE_DAY_LIMIT = 420

# ---------------- SAVED TRANSPORT ----------------

def get_saved_transport_candidates(trip, direction="outbound"):
    selected_types = TRAVEL_MODE_TYPES.get(
        trip.travel_mode,
        TRAVEL_MODE_TYPES["any"]
    )

    options = TransportOption.objects.filter(
        trip=trip,
        direction=direction,
    )

    candidates = []

    for option in options:
        # The user's own options always count; estimates follow the mode.
        if option.is_estimate and option.transport_type not in selected_types:
            continue

        duration = (
            option.arrival_time - option.departure_time
        ).total_seconds()

        if duration < 0 or option.estimated_cost <= 0:
            continue

        candidates.append({
            "kind": "estimate" if option.is_estimate else "saved",
            "direction": option.direction,
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
            "is_selected": option.is_selected,
            "is_estimate": option.is_estimate,
        })

    return candidates


TRAVEL_MODE_TYPES = {
    "self_vehicle": ["car", "bike"],
    "rented_vehicle": ["car"],
    "public_transport": ["flight", "train", "bus"],
    "any": ["flight", "train", "bus", "car", "bike"],
}


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


def normalized_penalties(values, higher_is_better=False):
    present = [float(value) for value in values if value is not None]
    if not present:
        return [None for _ in values]

    lowest = min(present)
    highest = max(present)
    if lowest == highest:
        return [None if value is None else 0.5 for value in values]

    return [
        None
        if value is None
        else (
            (highest - float(value)) / (highest - lowest)
            if higher_is_better
            else (float(value) - lowest) / (highest - lowest)
        )
        for value in values
    ]


def rank_transport_candidates(candidates):
    cost_penalties = normalized_penalties([
        candidate["estimated_cost"] for candidate in candidates
    ])
    duration_penalties = normalized_penalties([
        candidate["duration_seconds"] for candidate in candidates
    ])

    ranked = []
    for candidate, cost_penalty, duration_penalty in zip(
        candidates,
        cost_penalties,
        duration_penalties,
    ):
        score = 0.6 * cost_penalty + 0.4 * duration_penalty
        ranked.append({
            **candidate,
            "balanced_score": round(score, 4),
        })

    return sorted(
        ranked,
        key=lambda item: (
            item["balanced_score"],
            item["estimated_cost"],
            item["duration_seconds"],
        ),
    )


def nearest_sightseeing_distance(latitude, longitude, itinerary):
    def coordinate(item, key):
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    nearest = None
    for item in itinerary:
        nested_place = item.get("place") if isinstance(item, dict) else None
        place = (
            nested_place
            if nested_place is not None and not isinstance(nested_place, str)
            else item
        )
        place_latitude = coordinate(place, "latitude")
        place_longitude = coordinate(place, "longitude")
        if place_latitude is None or place_longitude is None:
            continue

        distance = haversine_coordinates(
            latitude,
            longitude,
            place_latitude,
            place_longitude,
        )
        place_name = coordinate(place, "name")
        if place_name is None and isinstance(item, dict):
            place_name = item.get("place")
        if nearest is None or distance < nearest[0]:
            nearest = (distance, place_name)

    if nearest is None:
        return None, None
    return round(nearest[0], 2), nearest[1]


def rank_accommodation_candidates(candidates, itinerary):
    price_penalties = normalized_penalties([
        candidate.price_per_night for candidate in candidates
    ])
    rating_penalties = normalized_penalties([
        candidate.rating for candidate in candidates
    ], higher_is_better=True)

    distances = []
    for candidate in candidates:
        if candidate.hotel_id and itinerary:
            distance, place_name = nearest_sightseeing_distance(
                candidate.hotel.latitude,
                candidate.hotel.longitude,
                itinerary,
            )
        else:
            distance, place_name = None, None
        distances.append((distance, place_name))

    distance_penalties = normalized_penalties([
        distance for distance, _ in distances
    ])

    ranked = []
    for candidate, price_penalty, rating_penalty, distance_penalty, distance in zip(
        candidates,
        price_penalties,
        rating_penalties,
        distance_penalties,
        distances,
    ):
        factors = [(0.5, price_penalty)]
        if rating_penalty is not None:
            factors.append((0.25, rating_penalty))
        if distance_penalty is not None:
            factors.append((0.25, distance_penalty))
        weight_total = sum(weight for weight, _ in factors)
        score = sum(weight * penalty for weight, penalty in factors) / weight_total
        ranked.append({
            "option": candidate,
            "balanced_score": round(score, 4),
            "nearest_sightseeing_distance_km": distance[0],
            "nearest_sightseeing_place": distance[1],
        })

    return sorted(
        ranked,
        key=lambda item: (
            item["balanced_score"],
            item["option"].price_per_night,
            -float(item["option"].rating or 0),
        ),
    )


def transport_candidate_summary(candidate):
    return {
        "id": candidate["model_id"],
        "type": candidate["transport_type"],
        "origin": candidate["origin"],
        "destination": candidate["destination"],
        "departure_time": timezone.localtime(candidate["departure_time"]).isoformat(),
        "arrival_time": timezone.localtime(candidate["arrival_time"]).isoformat(),
        "duration_seconds": candidate["duration_seconds"],
        "estimated_cost": str(candidate["estimated_cost"]),
        "currency": "INR",
        "provider": candidate["provider"],
        "booking_url": candidate["booking_url"],
        "direction": candidate.get("direction", "outbound"),
        "is_selected": candidate.get("is_selected", False),
        "is_estimate": candidate.get("is_estimate", False),
        "price_source": (
            "typical_fare_estimate" if candidate.get("is_estimate")
            else "user_entered_unverified"
        ),
    }


def accommodation_candidate_summary(ranked_candidate, nights):
    accommodation = ranked_candidate["option"]
    hotel = accommodation.hotel
    estimated_total = accommodation.price_per_night * nights
    return {
        "id": accommodation.id,
        "hotel_id": accommodation.hotel_id,
        "name": accommodation.name,
        "type": accommodation.accommodation_type,
        "location": accommodation.location,
        "price_per_night": str(accommodation.price_per_night),
        "rating": (
            str(accommodation.rating)
            if accommodation.rating is not None
            else None
        ),
        "nights": nights,
        "estimated_total": str(estimated_total),
        "booking_url": accommodation.booking_url,
        "is_selected": accommodation.is_selected,
        "is_estimate": accommodation.is_estimate,
        "price_source": (
            "typical_price_estimate" if accommodation.is_estimate
            else "user_entered_unverified"
        ),
        "latitude": str(hotel.latitude) if hotel else None,
        "longitude": str(hotel.longitude) if hotel else None,
        "nearest_sightseeing_distance_km": (
            ranked_candidate["nearest_sightseeing_distance_km"]
        ),
        "nearest_sightseeing_place": (
            ranked_candidate["nearest_sightseeing_place"]
        ),
    }


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


def calculate_attraction_cost(itinerary, travellers=1):
    """Entry fees are per person, so scale by the number of travellers."""
    per_person = sum(
        (
            item["place"].estimated_cost
            for item in itinerary
            if item["place"].estimated_cost > 0
        ),
        Decimal("0.00")
    )
    return per_person * max(int(travellers or 1), 1)


def choose_optimal_combination(
    transport_candidates,
    accommodation_candidates,
    nights,
    attraction_cost,
    budget,
    accommodation_scores=None,
):
    """
    Evaluate transport and accommodation together.

    Prefer combinations within budget. Among feasible combinations,
    balance total cost, transport quality and accommodation quality.

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

    # Prefer combinations within budget before applying the balanced score.
    pool = feasible if feasible else combinations

    if feasible:
        cost_penalties = normalized_penalties([
            item["known_total"] for item in pool
        ])
        for item, cost_penalty in zip(pool, cost_penalties):
            transport_penalty = (
                item["transport"].get("balanced_score", 0.5)
                if item["transport"] else 0.5
            )
            accommodation_penalty = (
                (accommodation_scores or {}).get(
                    item["accommodation"].id,
                    0.5,
                )
                if item["accommodation"] else 0.5
            )
            item["balanced_score"] = (
                0.55 * cost_penalty
                + 0.225 * transport_penalty
                + 0.225 * accommodation_penalty
            )

        selected = min(
            pool,
            key=lambda item: (
                item["balanced_score"],
                item["known_total"],
                item["duration_seconds"],
            ),
        )
    else:
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
            ),
        )

    return selected, len(combinations)


def calculate_unknown_costs(
    selected_transport,
    selected_accommodation,
    itinerary,
    dining_recommendations,
    nights,
    return_transport=None,
):
    unknown_components = []

    if not selected_transport:
        unknown_components.append("outbound transport")

    if not return_transport:
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
    return haversine_coordinates(
        place_a.latitude,
        place_a.longitude,
        place_b.latitude,
        place_b.longitude,
    )


def haversine_coordinates(latitude_a, longitude_a, latitude_b, longitude_b):
    lat1 = radians(float(latitude_a))
    lon1 = radians(float(longitude_a))
    lat2 = radians(float(latitude_b))
    lon2 = radians(float(longitude_b))
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

def select_sightseeing_pool(places, total_days):
    """Keep the best places a trip can use: top priority, near the centre."""
    limit = max(total_days, 1) * MAX_STOPS_PER_DAY
    if len(places) <= limit:
        return list(places)

    lat = sum(float(p.latitude) for p in places) / len(places)
    lon = sum(float(p.longitude) for p in places) / len(places)
    ranked = sorted(
        places,
        key=lambda place: (
            place_priority(place),
            haversine_coordinates(lat, lon, place.latitude, place.longitude),
            place.id,
        ),
    )
    return ranked[:limit]


def stops_per_active_day(itinerary):
    days = {item["day"] for item in itinerary}
    return len(itinerary) / len(days) if days else 0.0


def build_daily_schedule(places, trip, ai_days=None, windows=None, hotel=None):
    """Distribute sightseeing across trip days and group nearby places.

    windows: per-day (start, end) sightseeing times, None for a day lost to
    travel (see travel_stay.day_windows). hotel: object with latitude and
    longitude; each day's first stop is timed from it.
    """

    candidates = [
        place for place in places
        if (place.category or "").lower()
        not in MEAL_CATEGORIES | NON_VISIT_CATEGORIES
    ]

    if not candidates:
        return [], []

    total_days = max((trip.end_date - trip.start_date).days + 1, 1)
    if windows is None or len(windows) != total_days:
        windows = [(DAY_START_TIME, DAY_CLOSING_TIME)] * total_days

    def window_minutes(window):
        if not window:
            return 0
        start, end = window
        return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)

    # Days that can hold sightseeing, longest first: clusters fill these.
    usable_days = sorted(
        (index for index, window in enumerate(windows) if window),
        key=lambda index: (-min(window_minutes(windows[index]), ACTIVE_DAY_LIMIT), index),
    )
    if not usable_days:
        return [], list(candidates)

    def visit_duration(place):
        return max(int(place.time_required or 60), 30)

    # Fill days rather than spreading few places thinly: 38 places over
    # 20 days must give ~6 full days, not 2 stops a day.
    total_load = sum(visit_duration(place) for place in candidates)
    seed_count = min(
        len(usable_days),
        len(candidates),
        max(1, ceil(total_load / DAY_TARGET_VISIT_MINUTES)),
    )

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

    if ai_days is not None:
        day_clusters = list(enumerate(clusters))
    else:
        day_clusters = list(zip(usable_days, clusters))

    for day_index, cluster in sorted(day_clusters, key=lambda pair: pair[0]):
        window = windows[day_index]
        if not window:
            continue
        day_number = day_index + 1
        day_date = trip.start_date + timedelta(days=day_index)
        current_time = datetime.combine(day_date, window[0])
        day_closing = window[1]
        previous_place = hotel
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
                elif previous_place is hotel:
                    distance = haversine_distance(hotel, candidate)
                    travel_minutes = max(
                        10,
                        min(75, int((distance / 25) * 60)),
                    )
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
                lunch_start = datetime.combine(day_date, LUNCH_START_TIME)
                lunch_end = datetime.combine(day_date, LUNCH_END_TIME)

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

                if active_minutes + travel_minutes + duration > ACTIVE_DAY_LIMIT:
                    continue

                if end_time.time() > day_closing:
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


def build_activity_suggestions(
    places,
    itinerary,
    trip,
    remaining_budget,
    excluded_place_ids=None,
):
    """Offer one unplanned saved place per day with room, pending approval."""
    excluded_place_ids = {
        int(place_id)
        for place_id in (excluded_place_ids or [])
        if str(place_id).isdigit()
    }
    day_states = {
        day: {"used_minutes": 0, "items": []}
        for day in range(1, (trip.end_date - trip.start_date).days + 2)
    }
    scheduled_ids = set()

    for item in itinerary:
        if isinstance(item, dict):
            place = item.get("place")
            place_id = item.get("place_id") or getattr(place, "id", None)
            day = item.get("day")
            visit_minutes = item.get(
                "visit_minutes",
                getattr(place, "time_required", 60),
            )
            travel_minutes = item.get("travel_minutes_before", 0)
            end_value = item.get("end_time")
            latitude = item.get("latitude", getattr(place, "latitude", None))
            longitude = item.get("longitude", getattr(place, "longitude", None))
        else:
            place = item.place
            place_id = item.place_id
            day = item.day
            visit_minutes = max(int(place.time_required or 60), 30)
            travel_minutes = 0
            end_value = item.end_time
            latitude = place.latitude if place else None
            longitude = place.longitude if place else None

        if place_id is not None:
            scheduled_ids.add(int(place_id))

        try:
            day_state = day_states[int(day)]
        except (KeyError, TypeError, ValueError):
            continue

        try:
            visit_minutes = max(int(visit_minutes or 60), 30)
            travel_minutes = max(int(travel_minutes or 0), 0)
        except (TypeError, ValueError):
            visit_minutes = 60
            travel_minutes = 0

        def as_time(value):
            if isinstance(value, time):
                return value
            if isinstance(value, str):
                try:
                    return datetime.strptime(value[:5], "%H:%M").time()
                except ValueError:
                    return None
            return None

        day_state["used_minutes"] += visit_minutes + travel_minutes
        day_state["items"].append({
            "end_time": as_time(end_value),
            "latitude": latitude,
            "longitude": longitude,
        })

    try:
        remaining_budget = Decimal(str(remaining_budget))
    except (InvalidOperation, TypeError, ValueError):
        remaining_budget = Decimal("0.00")

    candidates = [
        place for place in places
        if place.id not in scheduled_ids
        and place.id not in excluded_place_ids
        and (place.category or "").lower() not in NON_VISIT_CATEGORIES
    ]
    suggestions = []
    ordered_days = sorted(
        day_states,
        key=lambda day: (
            bool(day_states[day]["items"]),
            day_states[day]["used_minutes"],
            day,
        ),
    )

    for day in ordered_days:
        state = day_states[day]
        day_date = trip.start_date + timedelta(days=day - 1)
        last_item = max(
            state["items"],
            key=lambda item: (
                item["end_time"] is not None,
                item["end_time"] or DAY_START_TIME,
            ),
            default=None,
        )
        anchor = (
            last_item
            if last_item
            and last_item["latitude"] is not None
            and last_item["longitude"] is not None
            else None
        )
        current_time = (
            datetime.combine(day_date, last_item["end_time"])
            if last_item and last_item["end_time"]
            else datetime.combine(day_date, DAY_START_TIME)
        )

        feasible = []
        for place in candidates:
            duration = max(int(place.time_required or 60), 30)
            distance = 0.0
            travel_minutes = 0
            if anchor and place.latitude is not None and place.longitude is not None:
                distance = haversine_coordinates(
                    anchor["latitude"],
                    anchor["longitude"],
                    place.latitude,
                    place.longitude,
                )
                travel_minutes = max(5, min(60, int((distance / 25) * 60)))

            start_time = current_time + timedelta(minutes=travel_minutes)
            lunch_start = datetime.combine(day_date, LUNCH_START_TIME)
            lunch_end = datetime.combine(day_date, LUNCH_END_TIME)
            end_time = start_time + timedelta(minutes=duration)
            if lunch_start <= start_time < lunch_end:
                start_time = lunch_end
            elif start_time < lunch_start and end_time > lunch_start:
                start_time = lunch_end

            end_time = start_time + timedelta(minutes=duration)
            if (
                state["used_minutes"] + travel_minutes + duration > ACTIVE_DAY_LIMIT
                or end_time.time() > DAY_CLOSING_TIME
            ):
                continue

            try:
                estimated_cost = Decimal(str(place.estimated_cost or 0))
            except (InvalidOperation, TypeError, ValueError):
                estimated_cost = Decimal("0.00")

            would_exceed_budget = (
                estimated_cost > 0 and estimated_cost > remaining_budget
            )
            budget_rank = (
                2 if would_exceed_budget
                else 0 if estimated_cost > 0
                else 1
            )
            feasible.append({
                "place": place,
                "duration": duration,
                "distance": distance,
                "travel_minutes": travel_minutes,
                "start_time": start_time,
                "end_time": end_time,
                "estimated_cost": estimated_cost,
                "would_exceed_budget": would_exceed_budget,
                "budget_rank": budget_rank,
            })

        if not feasible:
            continue

        selected = min(
            feasible,
            key=lambda option: (
                option["budget_rank"],
                place_priority(option["place"]),
                option["estimated_cost"],
                option["distance"],
                option["duration"],
                option["place"].name.lower(),
            ),
        )
        place = selected["place"]
        estimated_cost = selected["estimated_cost"]
        overage = max(estimated_cost - remaining_budget, Decimal("0.00"))
        suggestions.append({
            "id": place.id,
            "name": place.name,
            "category": place.category,
            "day": day,
            "start_time": selected["start_time"].strftime("%H:%M"),
            "duration_minutes": selected["duration"],
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
            "estimated_cost": (
                str(estimated_cost) if estimated_cost > 0 else None
            ),
            "would_exceed_budget": selected["would_exceed_budget"],
            "budget_overage": str(overage),
            "status": "pending_approval",
        })
        if estimated_cost > 0:
            remaining_budget = max(
                remaining_budget - estimated_cost, Decimal("0.00")
            )
        candidates.remove(place)

    return suggestions


# ---------------- FULL-DAY TIMELINE ----------------

def build_daily_timeline(itinerary, dining_recommendations, trip, travel=None):
    """Add lunch, dinner, and explicit free-time blocks to each trip day.

    travel: {"outbound": leg, "return": leg} where a leg has type,
    departure_time and arrival_time (ISO strings). Adds arrive/depart
    blocks and drops meals that fall outside the time at the destination.
    """
    from planner.travel_stay import (
        ARRIVAL_BUFFER_MINUTES,
        DEPARTURE_BUFFER_MINUTES,
    )

    def leg_local(leg, key):
        if not leg or not leg.get(key):
            return None
        moment = datetime.fromisoformat(leg[key])
        if timezone.is_aware(moment):
            moment = timezone.localtime(moment)
        return moment.replace(tzinfo=None)

    travel = travel or {}
    outbound_leg = travel.get("outbound")
    return_leg = travel.get("return")
    arrival_at = leg_local(outbound_leg, "arrival_time")
    departure_at = leg_local(return_leg, "departure_time")

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

        def anchor_of(item):
            if not item or item.get("latitude") in (None, "") or item.get(
                "longitude"
            ) in (None, ""):
                return None
            return {"latitude": item["latitude"], "longitude": item["longitude"]}

        first_anchor = anchor_of(first)
        last_anchor = anchor_of(last)

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

        cursor = 9 * 60
        closing = 21 * 60

        if arrival_at and arrival_at.date() == day_date:
            buffer = ARRIVAL_BUFFER_MINUTES.get(outbound_leg.get("type"), 60)
            arrive = arrival_at.hour * 60 + arrival_at.minute
            ready = min(arrive + buffer, 24 * 60 - 1)
            events = [
                event for event in events
                if event["type"] != "meal" or to_minutes(event["start_time"]) >= ready
            ]
            events.append({
                "type": "travel",
                "title": f"Arrive by {outbound_leg.get('type', 'transport')}",
                "start_time": format_time(arrive),
                "end_time": format_time(ready),
                "place": None,
                "note": "Arrival, transfer and hotel check-in.",
            })
            cursor = min(cursor, arrive)
        elif arrival_at and arrival_at.date() > day_date:
            events = [event for event in events if event["type"] != "meal"]

        if departure_at and departure_at.date() == day_date:
            buffer = DEPARTURE_BUFFER_MINUTES.get(return_leg.get("type"), 90)
            depart = departure_at.hour * 60 + departure_at.minute
            leave = max(depart - buffer, 0)
            events = [
                event for event in events
                if event["type"] != "meal" or to_minutes(event["end_time"]) <= leave
            ]
            events.append({
                "type": "travel",
                "title": f"Depart by {return_leg.get('type', 'transport')}",
                "start_time": format_time(leave),
                "end_time": format_time(depart),
                "place": None,
                "note": "Check out and reach the station, airport or road.",
            })
            closing = max(depart, cursor)

        events.sort(
            key=lambda event: to_minutes(event["start_time"])
        )

        complete_day = []

        for event in events:
            start = to_minutes(event["start_time"])
            end = to_minutes(event["end_time"])

            if start < cursor:
                # Avoid overlaps while preserving the activity duration.
                if event["type"] == "meal":
                    continue
                if event["type"] == "travel":
                    complete_day.append(event)
                    cursor = max(cursor, end)
                    continue

                duration = end - start
                start = cursor
                end = start + duration
                event["start_time"] = format_time(start)
                event["end_time"] = format_time(end)

                if end <= start:
                    continue

            if start >= closing:
                # Starts after the day window; keep it, no free-time padding.
                complete_day.append(event)
                cursor = max(cursor, end)
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

def get_hotel_recommendations(destination_name, itinerary):
    destination = find_destination_record(destination_name)
    if not destination:
        return []

    hotels = Hotel.objects.filter(destination=destination).order_by("name")

    recommendations = []
    for hotel in hotels:
        distance, place_name = nearest_sightseeing_distance(
            hotel.latitude,
            hotel.longitude,
            itinerary,
        )
        recommendations.append({
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
            "nearest_sightseeing_distance_km": distance,
            "nearest_sightseeing_place": place_name,
        })
    return recommendations


# ---------------- MAIN PLANNING ENGINE ----------------

@transaction.atomic
def leg_summary(candidate):
    if not candidate:
        return None
    return {
        "type": candidate["transport_type"],
        "provider": candidate["provider"],
        "departure_time": timezone.localtime(candidate["departure_time"]).isoformat(),
        "arrival_time": timezone.localtime(candidate["arrival_time"]).isoformat(),
    }


def option_list(candidates, chosen):
    chosen_id = chosen["model_id"] if chosen else None
    return [
        {
            **transport_candidate_summary(candidate),
            "id": candidate["model_id"],
            "in_plan": candidate["model_id"] == chosen_id,
        }
        for candidate in candidates
    ]


def trip_snapshot(trip):
    """What the plan was built from; a change means it is out of date."""
    return {
        "source": trip.source,
        "destination": trip.destination,
        "start_date": trip.start_date.isoformat(),
        "end_date": trip.end_date.isoformat(),
        "number_of_people": trip.number_of_people,
        "travel_mode": trip.travel_mode,
        "budget": str(trip.estimated_budget),
        "transport_ids": sorted(
            TransportOption.objects.filter(trip=trip).values_list("id", flat=True)
        ),
        "stay_ids": sorted(
            Accommodation.objects.filter(trip=trip).values_list("id", flat=True)
        ),
        "selected_transport": sorted(
            TransportOption.objects.filter(trip=trip, is_selected=True)
            .values_list("id", flat=True)
        ),
        "selected_stay": sorted(
            Accommodation.objects.filter(trip=trip, is_selected=True)
            .values_list("id", flat=True)
        ),
    }


def generate_optimal_plan(trip, use_ai=False, ai_time_budget=None):
    warnings = []

    if trip.end_date < trip.start_date:
        raise ValueError("Trip end date cannot be before start date.")

    if trip.number_of_people < 1:
        raise ValueError("At least one traveller is required.")

    nights = (trip.end_date - trip.start_date).days

    # 1. Retrieve saved destination places.
    destination = find_destination_record(trip.destination)
    places = (
        list(Place.objects.filter(destination=destination).order_by("name"))
        if destination else []
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

    total_days = (trip.end_date - trip.start_date).days + 1
    sightseeing_places = select_sightseeing_pool(sightseeing_places, total_days)

    # 3. Optionally use AI for day assignments; local rules remain authoritative.
    ai_days = None
    ai_provider = None
    if use_ai and not sightseeing_places:
        warnings.append(
            "AI day assignment was skipped because no sightseeing places "
            "are saved for this destination. Use Discover to add places, "
            "then regenerate the plan with AI enabled."
        )
    if use_ai and sightseeing_places:
        ai_days, ai_provider, ai_warning = build_ai_schedule(
            trip,
            sightseeing_places,
            time_budget=ai_time_budget,
        )
        if ai_warning:
            warnings.append(ai_warning)

    # 4. Suggest travel and stays (estimates), then rank everything saved.
    pool_for_centre = sightseeing_places or places
    centroid = (
        (
            sum(float(p.latitude) for p in pool_for_centre) / len(pool_for_centre),
            sum(float(p.longitude) for p in pool_for_centre) / len(pool_for_centre),
        )
        if pool_for_centre else None
    )
    travel_warning = ensure_transport_suggestions(trip, centroid)
    if travel_warning:
        warnings.append(travel_warning)
    ensure_hotel_suggestions(trip, destination, centroid)

    outbound_candidates = rank_transport_candidates(
        remove_duplicate_transport_candidates(
            get_saved_transport_candidates(trip, "outbound")
        )
    )
    return_candidates = rank_transport_candidates(
        remove_duplicate_transport_candidates(
            get_saved_transport_candidates(trip, "return")
        )
    )
    transport_candidates = outbound_candidates

    outbound_pool = (
        [c for c in outbound_candidates if c["is_selected"]]
        or outbound_candidates
    )
    for candidate in outbound_candidates:
        departs = timezone.localtime(candidate["departure_time"]).date()
        if candidate["is_selected"] and not (
            trip.start_date - timedelta(days=2) <= departs <= trip.start_date
        ):
            warnings.append(
                f"Your selected {candidate['transport_type']} departs on "
                f"{departs.isoformat()}, not around the trip start date."
            )

    accommodation_candidates = get_priced_accommodation_candidates(trip)
    accommodation_pool = (
        [a for a in accommodation_candidates if a.is_selected]
        or accommodation_candidates
    )

    def return_for(outbound):
        selected = [c for c in return_candidates if c["is_selected"]]
        if selected:
            return selected[0]
        if not return_candidates:
            return None
        if outbound:
            same_type = [
                c for c in return_candidates
                if c["transport_type"] == outbound["transport_type"]
            ]
            if same_type:
                return same_type[0]
        return return_candidates[0]

    def hotel_point(accommodation):
        if accommodation and accommodation.hotel_id:
            return accommodation.hotel
        return None

    def schedule(outbound, return_option, accommodation):
        windows = day_windows(trip, outbound, return_option)
        hotel = hotel_point(accommodation)
        mode = "rules"
        notes = []
        if ai_days is not None:
            ai_itinerary, ai_unscheduled = build_daily_schedule(
                sightseeing_places, trip, ai_days=ai_days,
                windows=windows, hotel=hotel,
            )
            standard_itinerary, standard_unscheduled = build_daily_schedule(
                sightseeing_places, trip, windows=windows, hotel=hotel,
            )
            too_sparse = (
                stops_per_active_day(ai_itinerary)
                < 0.6 * stops_per_active_day(standard_itinerary)
            )
            if len(ai_unscheduled) > len(standard_unscheduled) or too_sparse:
                notes.append(
                    "The AI day assignments did not fit the schedule as well as "
                    "the standard planner, so the standard itinerary was used."
                )
                return standard_itinerary, standard_unscheduled, mode, notes, windows
            return ai_itinerary, ai_unscheduled, "ai", notes, windows
        result, unscheduled = build_daily_schedule(
            sightseeing_places, trip, windows=windows, hotel=hotel,
        )
        return result, unscheduled, mode, notes, windows

    # First pass with the preferred travel and stay, to rank stays by
    # proximity and to know attraction costs.
    first_outbound = outbound_pool[0] if outbound_pool else None
    first_return = return_for(first_outbound)
    first_stay = (
        accommodation_pool[0]
        if accommodation_pool and accommodation_pool[0].is_selected
        else None
    )
    itinerary, unscheduled_places, planner_mode, schedule_notes, windows = schedule(
        first_outbound, first_return, first_stay
    )

    attraction_cost = calculate_attraction_cost(
        itinerary, trip.number_of_people
    )

    if not outbound_candidates:
        warnings.append(
            "No priced transport option is saved. Search externally, "
            "then add a verified option to compare transport."
        )

    ranked_accommodations = rank_accommodation_candidates(
        accommodation_candidates,
        itinerary,
    )
    accommodation_scores = {
        item["option"].id: item["balanced_score"]
        for item in ranked_accommodations
    }

    if nights > 0 and not accommodation_candidates:
        warnings.append(
            "No priced accommodation option is saved for this trip. "
            "Add a verified nightly quote to compare stays."
        )

    # 5. Prefer budget-feasible combinations, then balance cost and quality.
    combination, combinations_considered = choose_optimal_combination(
        transport_candidates=outbound_pool,
        accommodation_candidates=accommodation_pool,
        nights=nights,
        attraction_cost=attraction_cost,
        budget=trip.estimated_budget,
        accommodation_scores=accommodation_scores,
    )

    selected_transport = (
        combination["transport"] if combination else None
    )
    selected_accommodation = (
        combination["accommodation"] if combination else None
    )
    selected_return = return_for(selected_transport)

    # 6. Rebuild the days around the final arrival, departure and hotel.
    if (
        selected_transport is not first_outbound
        or selected_return is not first_return
        or getattr(hotel_point(selected_accommodation), "id", None)
        != getattr(hotel_point(first_stay), "id", None)
    ):
        itinerary, unscheduled_places, planner_mode, schedule_notes, windows = schedule(
            selected_transport, selected_return, selected_accommodation
        )
        attraction_cost = calculate_attraction_cost(
            itinerary, trip.number_of_people
        )
    warnings.extend(schedule_notes)

    lost_days = [index + 1 for index, window in enumerate(windows) if not window]
    if lost_days and (selected_transport or selected_return):
        warnings.append(
            "No sightseeing on day "
            + ", ".join(str(day) for day in lost_days)
            + " because of travel times."
        )

    if unscheduled_places:
        warnings.append(
            f"{len(unscheduled_places)} sightseeing places "
            "could not fit into the available itinerary days."
        )

    usable_day_count = sum(1 for window in windows if window)
    free_days = usable_day_count - len({item["day"] for item in itinerary})
    if itinerary and free_days > 0:
        warnings.append(
            f"Only {len(sightseeing_places)} sightseeing places are saved for "
            f"this city, enough for full days on "
            f"{usable_day_count - free_days} of {usable_day_count} days; the other "
            f"{free_days} days are left free. Use Discover to add more places."
        )

    transport_cost = (
        (selected_transport["estimated_cost"] if selected_transport else Decimal("0.00"))
        + (selected_return["estimated_cost"] if selected_return else Decimal("0.00"))
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
        return_transport=selected_return,
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
        # Serialize concurrent generations for the same trip so only one
        # optimal plan row is ever created.
        type(trip).objects.select_for_update().filter(pk=trip.pk).first()
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
                "place_id": item["place"].id,
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

    # 10. Format the balanced recommendation and up to three alternatives.
    transport_summary = (
        transport_candidate_summary(selected_transport)
        if selected_transport else None
    )
    selected_accommodation_rank = next(
        (
            item for item in ranked_accommodations
            if selected_accommodation and item["option"].id == selected_accommodation.id
        ),
        None,
    )
    accommodation_summary = (
        accommodation_candidate_summary(
            selected_accommodation_rank,
            nights,
        )
        if selected_accommodation_rank else None
    )

    if accommodation_summary:
        accommodation_summary["price_warning"] = (
            "Verify this nightly price against an actual hotel quote."
        )

    transport_alternatives = [
        transport_candidate_summary(candidate)
        for candidate in transport_candidates
        if not selected_transport
        or candidate["model_id"] != selected_transport.get("model_id")
    ][:3]
    accommodation_alternatives = [
        accommodation_candidate_summary(candidate, nights)
        for candidate in ranked_accommodations
        if not selected_accommodation
        or candidate["option"].id != selected_accommodation.id
    ][:3]

    travel_legs = {
        "outbound": leg_summary(selected_transport),
        "return": leg_summary(selected_return),
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
            "Budget-feasible combinations are preferred. Among them, "
            "the planner balances total known cost, transport cost and "
            "duration, and accommodation price, rating and available "
            "straight-line proximity to planned sightseeing. If none "
            "fits, the cheapest known-cost combination is returned."
        ),
        "combinations_considered": combinations_considered,
        "note": (
            "This is a partial trip estimate. Local transport, meals "
            "and unpriced attraction fees are not included. Estimated "
            "fares and stay prices must be verified before booking."
        ),
    }

    if not selected_accommodation and nights > 0:
        cost_breakdown["accommodation"] = None

    if not selected_transport:
        cost_breakdown["transport"] = None

    # 13. Add transparent warnings.
    if selected_accommodation and selected_accommodation.is_estimate:
        warnings.append(
            "The stay price is a typical estimate for "
            f"{ceil(trip.number_of_people / 2)} room(s). Check the real "
            "price and add your booking to replace it."
        )
    elif selected_accommodation:
        warnings.append(
            "Accommodation price is user-entered and unverified. "
            "Confirm the actual hotel quotation before relying on it."
        )

    if any(leg and leg["is_estimate"] for leg in (selected_transport, selected_return)):
        warnings.append(
            "Travel fares and times are typical estimates. Check the "
            "booking link and add your real ticket to replace them."
        )

    if not selected_transport and not travel_warning and outbound_candidates:
        warnings.append("Outbound transport has no selected priced option.")

    return {
        "message": "Optimal travel plan generated successfully.",
        "plan_id": plan.id,
        "strategy": {
            "name": "Balanced, budget-aware recommendation",
            "selection_method": (
                "Prefer combinations within budget, then balance total "
                "cost, transport duration, accommodation rating and "
                "available straight-line proximity."
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
        "return_transport": (
            transport_candidate_summary(selected_return)
            if selected_return else None
        ),
        "transport_alternatives": transport_alternatives,
        "transport_options": {
            "outbound": option_list(outbound_candidates, selected_transport),
            "return": option_list(return_candidates, selected_return),
        },
        "accommodation_options": [
            {
                "id": option.id,
                "name": option.name,
                "accommodation_type": option.accommodation_type,
                "price_per_night": str(option.price_per_night),
                "rating": option.rating,
                "booking_url": option.booking_url,
                "is_estimate": option.is_estimate,
                "is_selected": option.is_selected,
                "in_plan": bool(
                    selected_accommodation
                    and option.id == selected_accommodation.id
                ),
            }
            for option in accommodation_candidates
        ],
        "travel_legs": travel_legs,
        "trip_snapshot": trip_snapshot(trip),
        "accommodation": accommodation_summary,
        "accommodation_alternatives": accommodation_alternatives,
        "hotel_recommendations": get_hotel_recommendations(
            trip.destination,
            itinerary,
        ),
        "dining_recommendations": dining_recommendations,
        "nearby_recommendations": nearby_recommendations,
        "estimated_cost": cost_breakdown,
        "itinerary": saved_itinerary,
        "daily_schedule": build_daily_timeline(
            saved_itinerary,
            dining_recommendations,
            trip,
            travel=travel_legs,
        ),
        "unscheduled_places": [
            place.name for place in unscheduled_places
        ],
        "activity_suggestions": build_activity_suggestions(
            places,
            itinerary,
            trip,
            trip.estimated_budget - known_total,
        ),
        "dismissed_activity_ids": [],
        "warnings": warnings,
        "map_data_source": "OpenStreetMap",
        "map_attribution": "© OpenStreetMap contributors",
    }
