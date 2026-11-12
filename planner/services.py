
import logging

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from math import radians, sin, cos, asin, sqrt

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from destinations.models import Place, Hotel
from integrations.flights import search_flights
from planner.models import Plan, ItineraryItem
from trips.models import TransportOption, Accommodation


logger = logging.getLogger(__name__)


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


# ---------------- FLIGHT CANDIDATES ----------------

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

            if price < 0:
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

        if duration < 0:
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


# ---------------- TRANSPORT OPTIMIZATION ----------------

def choose_optimal_transport(candidates):

    if not candidates:
        return None

    min_cost = min(
        item["estimated_cost"] for item in candidates
    )

    max_cost = max(
        item["estimated_cost"] for item in candidates
    )

    min_duration = min(
        item["duration_seconds"] for item in candidates
    )

    max_duration = max(
        item["duration_seconds"] for item in candidates
    )

    def calculate_score(item):

        if max_cost == min_cost:
            cost_score = 0
        else:
            cost_score = float(
                (item["estimated_cost"] - min_cost)
                / (max_cost - min_cost)
            )

        if max_duration == min_duration:
            time_score = 0
        else:
            time_score = (
                (item["duration_seconds"] - min_duration)
                / (max_duration - min_duration)
            )

        return (0.65 * cost_score) + (0.35 * time_score)

    return min(candidates, key=calculate_score)


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
        for place in dining_places[:20]
    ]


# ---------------- SIGHTSEEING ITINERARY ----------------

def build_daily_schedule(places, trip):

    # Never schedule restaurants or cafes as sightseeing activities.
    remaining = [
        place for place in places
        if (place.category or "").lower()
        not in MEAL_CATEGORIES | NON_VISIT_CATEGORIES
    ]

    scheduled = []

    total_days = (trip.end_date - trip.start_date).days + 1

    for day_number in range(1, total_days + 1):

        day_date = trip.start_date + timedelta(days=day_number - 1)

        current_time = datetime.combine(
            day_date,
            time(9, 0)
        )

        previous_place = None
        day_active_minutes = 0
        lunch_taken = False
        order = 1

        while remaining:

            # First activity: choose the highest-priority attraction.
            if previous_place is None:

                next_place = min(
                    remaining,
                    key=place_priority
                )

                travel_minutes = 0
                distance = 0

            else:

                # Subsequent activities: choose the nearest remaining place.
                next_place = min(
                    remaining,
                    key=lambda place: haversine_distance(
                        previous_place,
                        place
                    )
                )

                distance = haversine_distance(
                    previous_place,
                    next_place
                )

                # Approximate local travel time.
                # This is not a live road-route estimate.
                travel_minutes = max(
                    5,
                    min(60, int((distance / 25) * 60))
                )

            visit_minutes = max(
                int(next_place.time_required or 60),
                30
            )

            # Do not exceed seven active hours of daily activities.
            if (
                day_active_minutes
                + travel_minutes
                + visit_minutes
                > 420
            ):
                break

            start = current_time + timedelta(
                minutes=travel_minutes
            )

            end = start + timedelta(
                minutes=visit_minutes
            )

            # Insert an approximate lunch break between 12:30 and 13:15.
            if not lunch_taken:

                lunch_start = datetime.combine(
                    day_date,
                    time(12, 30)
                )

                lunch_end = datetime.combine(
                    day_date,
                    time(13, 15)
                )

                crosses_lunch = (
                    start < lunch_end and end > lunch_start
                )

                starts_during_lunch = (
                    lunch_start <= start < lunch_end
                )

                if crosses_lunch or starts_during_lunch:

                    start = max(start, lunch_end)

                    end = start + timedelta(
                        minutes=visit_minutes
                    )

                    lunch_taken = True

                elif start >= lunch_end:
                    lunch_taken = True

            # Do not schedule activities after 5:30 PM.
            if end.time() > time(17, 30):
                break

            scheduled.append({
                "day": day_number,
                "place": next_place,
                "start_time": start.time(),
                "end_time": end.time(),
                "order": order,
                "visit_minutes": visit_minutes,
                "travel_minutes_before": travel_minutes,
                "distance_before_km": round(distance, 2),
            })

            day_active_minutes += (
                travel_minutes + visit_minutes
            )

            current_time = end
            previous_place = next_place

            remaining.remove(next_place)
            order += 1

    return scheduled, remaining


# ---------------- HOTEL RECOMMENDATIONS ----------------

def get_hotel_recommendations(destination_name):

    hotels = Hotel.objects.filter(
        destination__name__iexact=destination_name
    ).order_by("name")[:10]

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

def generate_optimal_plan(trip):

    warnings = []

    if trip.end_date < trip.start_date:
        raise ValueError(
            "Trip end date cannot be before start date."
        )

    # 1. Fetch saved destination places.
    places = list(
        Place.objects.filter(
            destination__name__iexact=trip.destination
        ).order_by("name")
    )

    if not places:
        warnings.append(
            "No saved places found for this destination."
        )

    # 2. Separate dining places from sightseeing.
    dining_recommendations = get_dining_recommendations(
        places
    )

    sightseeing_places = [
        place for place in places
        if (place.category or "").lower()
        not in MEAL_CATEGORIES | NON_VISIT_CATEGORIES
    ]

    if not sightseeing_places:
        warnings.append(
            "No sightseeing places were found for this destination."
        )

    # 3. Find available transport options.
    live_flights, flight_warning = get_flight_candidates(trip)

    if flight_warning:
        warnings.append(flight_warning)

    saved_transport = get_saved_transport_candidates(trip)

    candidates = saved_transport + live_flights

    selected_transport = choose_optimal_transport(candidates)

    if selected_transport:
        if selected_transport["kind"] == "provider":
            selected_transport = save_selected_flight(
                trip,
                selected_transport
            )
    else:
        warnings.append(
            "No matching transport option found. "
            "Transport cost is excluded."
        )

    # 4. Retrieve hotel recommendations.
    hotels = get_hotel_recommendations(
        trip.destination
    )

    if not hotels:
        warnings.append(
            "No saved hotels found for this destination."
        )

    # 5. Use an accommodation only if a price is known.
    selected_accommodation = (
        Accommodation.objects.filter(
            trip=trip,
            price_per_night__gt=0
        )
        .order_by("price_per_night")
        .first()
    )

    nights = max(
        (trip.end_date - trip.start_date).days,
        0
    )

    accommodation_cost = Decimal("0.00")

    if selected_accommodation:
        accommodation_cost = (
            selected_accommodation.price_per_night * nights
        )
    else:
        warnings.append(
            "No accommodation with a known price is saved for this trip."
        )

    # 6. Generate sightseeing schedule only.
    itinerary, unscheduled_places = build_daily_schedule(
        sightseeing_places,
        trip
    )

    if unscheduled_places:
        warnings.append(
            f"{len(unscheduled_places)} sightseeing places "
            "could not fit into the available itinerary days."
        )

    # 7. Calculate known costs.
    transport_cost = Decimal("0.00")

    if selected_transport:
        transport_cost = selected_transport["estimated_cost"]

    attraction_cost = sum(
        (
            item["place"].estimated_cost
            for item in itinerary
            if item["place"].estimated_cost > 0
        ),
        Decimal("0.00")
    )

    estimated_total = (
        transport_cost
        + accommodation_cost
        + attraction_cost
    )

    unknown_components = []

    if not selected_transport:
        unknown_components.append("transport")

    if not selected_accommodation:
        unknown_components.append("accommodation")

    if any(
        item["place"].estimated_cost <= 0
        for item in itinerary
    ):
        unknown_components.append("attraction entry costs")

    # 8. Calculate activity and local-travel effort.
    total_minutes = sum(
        item["visit_minutes"] + item["travel_minutes_before"]
        for item in itinerary
    )

    estimated_effort = round(
        total_minutes / 60,
        2
    )

    # 9. Save or update the plan and its itinerary.
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

            plan.estimated_cost = estimated_total
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
                estimated_cost=estimated_total,
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

    # 10. Format transport details.
    transport_summary = None

    if selected_transport:

        transport_summary = {
            "type": selected_transport["transport_type"],
            "origin": selected_transport["origin"],
            "destination": selected_transport["destination"],
            "departure_time": (
                selected_transport["departure_time"].isoformat()
            ),
            "arrival_time": (
                selected_transport["arrival_time"].isoformat()
            ),
            "duration_seconds": selected_transport["duration_seconds"],
            "estimated_cost": str(
                selected_transport["estimated_cost"]
            ),
            "provider": selected_transport["provider"],
            "booking_url": selected_transport["booking_url"],
            "model_id": selected_transport.get("model_id"),
        }

    # 11. Format accommodation details.
    accommodation_summary = None

    if selected_accommodation:

        accommodation_summary = {
            "name": selected_accommodation.name,
            "type": selected_accommodation.accommodation_type,
            "price_per_night": str(
                selected_accommodation.price_per_night
            ),
            "nights": nights,
            "estimated_total": str(accommodation_cost),
            "booking_url": selected_accommodation.booking_url,
        }

    # 12. Determine budget status.
    if unknown_components:
        budget_status = "incomplete"
    elif estimated_total > trip.estimated_budget:
        budget_status = "over_budget"
    else:
        budget_status = "within_budget"

    return {
        "message": "Optimal travel plan generated successfully.",
        "plan_id": plan.id,
        "strategy": {
            "name": "Optimal",
            "transport_cost_weight": 0.65,
            "transport_time_weight": 0.35,
        },
        "trip": {
            "source": trip.source,
            "destination": trip.destination,
            "start_date": trip.start_date.isoformat(),
            "end_date": trip.end_date.isoformat(),
            "number_of_people": trip.number_of_people,
            "budget": str(trip.estimated_budget),
        },
        "transport": transport_summary,
        "accommodation": accommodation_summary,
        "hotel_recommendations": hotels,
        "dining_recommendations": dining_recommendations,
        "estimated_cost": {
            "currency": "INR",
            "known_total": str(estimated_total),
            "transport": str(transport_cost),
            "accommodation": str(accommodation_cost),
            "attractions": str(attraction_cost),
            "budget_status": budget_status,
            "unknown_components": unknown_components,
            "note": (
                "Partial estimate only. Missing prices are not treated "
                "as zero-cost services."
            ),
        },
        "itinerary": saved_itinerary,
        "unscheduled_places": [
            place.name for place in unscheduled_places
        ],
        "warnings": warnings,
        "map_data_source": "OpenStreetMap",
        "map_attribution": "© OpenStreetMap contributors",
    }
