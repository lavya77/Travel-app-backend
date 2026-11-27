
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_time

from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)

from config.throttles import ExternalApiThrottle
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from integrations.save_places import (
    DestinationDataUnavailableError,
    DestinationNotFoundError,
    ensure_destination_places,
    find_destination_record,
)
from trips.models import Trip
from destinations.models import Place

from .models import Plan, ItineraryItem
from .services import (
    build_activity_suggestions,
    build_daily_timeline,
    generate_optimal_plan,
    LUNCH_END_TIME,
    LUNCH_START_TIME,
    haversine_coordinates,
    nearest_sightseeing_distance,
    trip_snapshot,
)

# gunicorn kills a worker after 60s; finish well before that.
GENERATE_TIME_BUDGET_SECONDS = 50


logger = logging.getLogger(__name__)


NON_ITINERARY_CATEGORIES = {
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


def get_latest_plan_for_trip(trip, lock=False):
    queryset = Plan.objects.filter(
        trip=trip,
        plan_type="optimal"
    )

    if lock:
        queryset = queryset.select_for_update()

    plan = queryset.order_by("-created_at").first()

    if not plan:
        raise Http404("No generated plan found for this trip.")

    return plan


def validate_trip_day(trip, day):
    try:
        day = int(day)
    except (TypeError, ValueError):
        return None

    total_days = (trip.end_date - trip.start_date).days + 1

    return day if 1 <= day <= total_days else None


def json_object_error(request):
    """Return a 400 response when the body is not a JSON object."""
    if isinstance(request.data, dict):
        return None
    return Response(
        {"error": "The request body must be a JSON object."},
        status=status.HTTP_400_BAD_REQUEST,
    )


def travel_minutes_between(place_a, place_b):
    if (
        not place_a or not place_b
        or place_a.latitude is None or place_a.longitude is None
        or place_b.latitude is None or place_b.longitude is None
    ):
        return 0
    distance = haversine_coordinates(
        place_a.latitude, place_a.longitude,
        place_b.latitude, place_b.longitude,
    )
    return max(5, min(60, int((distance / 25) * 60)))


def reflow_day_times(items, day_date):
    """Re-time items in their new order: keep durations, add travel, skip lunch.

    Returns False (and changes nothing) if the day would run past midnight.
    """
    if not items:
        return True

    start_of_day = min(item.start_time for item in items)
    cursor = datetime.combine(day_date, start_of_day)
    lunch_start = datetime.combine(day_date, LUNCH_START_TIME)
    lunch_end = datetime.combine(day_date, LUNCH_END_TIME)
    lunch_taken = False
    previous_place = None
    new_times = []

    for item in items:
        duration = (
            datetime.combine(day_date, item.end_time)
            - datetime.combine(day_date, item.start_time)
        )
        start = cursor + timedelta(
            minutes=travel_minutes_between(previous_place, item.place)
        )
        if not lunch_taken:
            if lunch_start <= start < lunch_end or (
                start < lunch_start and start + duration > lunch_start
            ):
                start = lunch_end
                lunch_taken = True
            elif start >= lunch_end:
                lunch_taken = True
        end = start + duration
        if end.date() != day_date or start.date() != day_date:
            return False
        new_times.append((start.time(), end.time()))
        cursor = end
        previous_place = item.place or previous_place

    for item, (start, end) in zip(items, new_times):
        item.start_time = start
        item.end_time = end
    ItineraryItem.objects.bulk_update(items, ["start_time", "end_time"])
    return True


PAST_MIDNIGHT_ERROR = (
    "This activity would run past midnight. "
    "Choose an earlier time or another day."
)


def assign_day_orders(items, day):
    """Re-number a day's items without colliding with temporary order values."""
    items = list(items)

    for index, item in enumerate(items, start=1):
        item.day = day
        item.order = 100000 + index

    if items:
        ItineraryItem.objects.bulk_update(items, ["day", "order"])

    for index, item in enumerate(items, start=1):
        item.order = index

    if items:
        ItineraryItem.objects.bulk_update(items, ["order"])


def attach_activity_suggestions(response_data, trip, itinerary):
    destination = find_destination_record(trip.destination)
    places = (
        list(Place.objects.filter(destination=destination).order_by("name"))
        if destination else []
    )
    cost_data = response_data.get("estimated_cost", {})
    remaining_budget = cost_data.get("remaining_known_budget")
    if remaining_budget is None:
        try:
            known_total = Decimal(str(cost_data.get("known_total", 0)))
        except (InvalidOperation, TypeError, ValueError):
            known_total = Decimal("0.00")
        remaining_budget = trip.estimated_budget - known_total

    response_data["activity_suggestions"] = build_activity_suggestions(
        places,
        itinerary,
        trip,
        remaining_budget,
        excluded_place_ids=response_data.get("dismissed_activity_ids", []),
    )
    response_data.setdefault("dismissed_activity_ids", [])
    return response_data


def refresh_plan_response(plan):
    """Refresh itinerary, full-day timeline and known-cost totals in snapshot."""
    response_data = plan.response_data or {}

    previous_itinerary = {
        item.get("id"): item
        for item in response_data.get("itinerary", [])
        if item.get("id") is not None
    }

    items = (
        ItineraryItem.objects.filter(plan=plan)
        .select_related("place")
        .order_by("day", "order")
    )

    updated_itinerary = []
    attraction_cost = Decimal("0.00")
    has_unknown_attraction_cost = False
    travellers = max(int(plan.trip.number_of_people or 1), 1)
    previous_by_day = {}

    for item in items:
        place = item.place
        old_data = previous_itinerary.get(item.id, {})

        if place:
            place_name = place.name
            category = place.category
            latitude = str(place.latitude)
            longitude = str(place.longitude)

            if place.estimated_cost > 0:
                attraction_cost += place.estimated_cost * travellers
            else:
                has_unknown_attraction_cost = True

            visit_minutes = max(int(place.time_required or 60), 30)
        else:
            place_name = None
            category = None
            latitude = None
            longitude = None
            visit_minutes = 60

        previous_place = previous_by_day.get(item.day)
        if place and previous_place:
            distance_before = haversine_coordinates(
                previous_place.latitude,
                previous_place.longitude,
                place.latitude,
                place.longitude,
            )
            travel_before = max(5, min(60, int((distance_before / 25) * 60)))
            distance_before = round(distance_before, 2)
        else:
            distance_before = 0
            travel_before = 0
        if place:
            previous_by_day[item.day] = place

        updated_itinerary.append({
            "id": item.id,
            "place_id": item.place_id,
            "day": item.day,
            "place": place_name,
            "category": category,
            "start_time": item.start_time.strftime("%H:%M"),
            "end_time": item.end_time.strftime("%H:%M"),
            "order": item.order,
            "latitude": latitude,
            "longitude": longitude,
            "visit_minutes": old_data.get("visit_minutes", visit_minutes),
            "travel_minutes_before": travel_before,
            "distance_before_km": distance_before,
        })

    response_data["itinerary"] = updated_itinerary

    distance_options = [
        response_data.get("accommodation"),
        *response_data.get("accommodation_alternatives", []),
        *response_data.get("hotel_recommendations", []),
    ]
    for option in distance_options:
        if not option or option.get("latitude") is None or option.get("longitude") is None:
            continue
        distance, place_name = nearest_sightseeing_distance(
            option["latitude"],
            option["longitude"],
            updated_itinerary,
        )
        option["nearest_sightseeing_distance_km"] = distance
        option["nearest_sightseeing_place"] = place_name

    scheduled_names = {
        item["place"] for item in updated_itinerary if item["place"]
    }

    response_data["unscheduled_places"] = [
        name for name in response_data.get("unscheduled_places", [])
        if name not in scheduled_names
    ]

    cost_data = response_data.get("estimated_cost", {})

    def safe_decimal(value):
        if value in (None, ""):
            return Decimal("0.00")
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.00")

    transport_raw = cost_data.get("transport")
    accommodation_raw = cost_data.get("accommodation")

    known_total = (
        safe_decimal(transport_raw)
        + safe_decimal(accommodation_raw)
        + attraction_cost
    )

    # Use the live trip, not the snapshot taken when the plan was generated.
    trip = plan.trip
    trip_data = response_data.setdefault("trip", {})
    trip_data.update({
        "destination": trip.destination,
        "start_date": trip.start_date.isoformat(),
        "end_date": trip.end_date.isoformat(),
        "number_of_people": trip.number_of_people,
        "nights": (trip.end_date - trip.start_date).days,
        "budget": str(trip.estimated_budget),
    })
    budget = safe_decimal(trip.estimated_budget)

    unknown_components = list(cost_data.get("unknown_components", []))

    if has_unknown_attraction_cost:
        if "attraction entry fees" not in unknown_components:
            unknown_components.append("attraction entry fees")
    else:
        unknown_components = [
            value for value in unknown_components
            if value != "attraction entry fees"
        ]

    if known_total > budget:
        budget_status = "known_costs_over_budget"
    elif unknown_components:
        budget_status = "incomplete"
    else:
        budget_status = "within_budget"

    cost_data.update({
        "known_total": str(known_total),
        "attractions": str(attraction_cost),
        "remaining_known_budget": str(budget - known_total),
        "budget_status": budget_status,
        "within_known_budget": known_total <= budget,
        "unknown_components": unknown_components,
    })

    response_data["estimated_cost"] = cost_data
    response_data = attach_activity_suggestions(
        response_data,
        plan.trip,
        updated_itinerary,
    )

    # Keep full-day timeline synchronized after add/remove/move/reorder.
    response_data["daily_schedule"] = build_daily_timeline(
        updated_itinerary,
        response_data.get("dining_recommendations", []),
        plan.trip,
        travel=response_data.get("travel_legs"),
    )

    plan.estimated_cost = known_total
    plan.response_data = response_data
    plan.save(update_fields=["estimated_cost", "response_data"])

    return response_data


def get_request_plan(request, trip_id, lock=False):
    trip = get_object_or_404(
        Trip,
        id=trip_id,
        user=request.user
    )

    plan = get_latest_plan_for_trip(trip, lock=lock)

    if not plan.response_data:
        return trip, None

    return trip, plan


# ---------------- GENERATE PLAN ----------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ExternalApiThrottle])
def generate_plan(request, trip_id):
    trip = get_object_or_404(
        Trip,
        id=trip_id,
        user=request.user
    )

    if not isinstance(request.data, dict):
        return Response(
            {"error": "The request body must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    use_ai = request.data.get("use_ai", False)
    if not isinstance(use_ai, bool):
        return Response(
            {"error": "use_ai must be a boolean."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if trip.end_date < trip.start_date:
        return Response(
            {"error": "Trip end date cannot be before start date."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if trip.number_of_people < 1:
        return Response(
            {"error": "At least one traveller is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    started = monotonic()
    try:
        trip_days = (trip.end_date - trip.start_date).days + 1
        data_preparation = ensure_destination_places(
            trip.destination,
            # About five stops a day for the whole trip.
            min_sightseeing=trip_days * 5,
        )
    except DestinationNotFoundError as error:
        return Response(
            {"error": str(error)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except DestinationDataUnavailableError as error:
        logger.warning(
            "Destination preparation failed (%s)",
            type(error.__cause__).__name__ if error.__cause__ else type(error).__name__,
        )
        return Response(
            {"error": str(error)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        with transaction.atomic():
            result = generate_optimal_plan(
                trip,
                use_ai=use_ai,
                ai_time_budget=(
                    GENERATE_TIME_BUDGET_SECONDS
                    - (monotonic() - started)
                ),
            )
            result["data_preparation"] = data_preparation

            plan = get_object_or_404(
                Plan.objects.select_for_update(),
                id=result["plan_id"],
                trip=trip,
                plan_type="optimal",
            )

            plan.response_data = result
            plan.save(update_fields=["response_data"])

        return Response(
            result,
            status=status.HTTP_201_CREATED
        )

    except ValueError as error:
        return Response(
            {"error": str(error)},
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:
        logger.exception(
            "Optimal plan generation failed for trip %s",
            trip_id
        )

        return Response(
            {"error": "Unable to generate the travel plan."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ---------------- LATEST PLAN ----------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def latest_plan(request, trip_id):
    trip = get_object_or_404(
        Trip,
        id=trip_id,
        user=request.user
    )

    plan = (
        Plan.objects.filter(
            trip=trip,
            plan_type="optimal"
        )
        .prefetch_related("itinerary_items__place")
        .order_by("-created_at")
        .first()
    )

    if not plan:
        return Response(
            {"error": "No generated plan found for this trip."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not plan.response_data:
        return Response(
            {
                "error": (
                    "This plan needs to be regenerated before its "
                    "complete response can be retrieved."
                ),
                "message": "Send a POST request to the generate endpoint.",
            },
            status=status.HTTP_409_CONFLICT,
        )

    response_data = dict(plan.response_data)
    if "activity_suggestions" not in response_data:
        plan_items = list(
            plan.itinerary_items
            .select_related("place")
            .order_by("day", "order")
        )
        snapshot_items = {
            item.get("id"): item
            for item in response_data.get("itinerary", [])
            if item.get("id") is not None
        }
        itinerary = []
        for item in plan_items:
            snapshot = snapshot_items.get(item.id, {})
            place = item.place
            itinerary.append({
                **snapshot,
                "place_id": item.place_id,
                "place": place,
                "day": item.day,
                "start_time": snapshot.get(
                    "start_time",
                    item.start_time.strftime("%H:%M"),
                ),
                "end_time": snapshot.get(
                    "end_time",
                    item.end_time.strftime("%H:%M"),
                ),
                "visit_minutes": snapshot.get(
                    "visit_minutes",
                    max(int(place.time_required or 60), 30) if place else 60,
                ),
                "travel_minutes_before": snapshot.get(
                    "travel_minutes_before",
                    0,
                ),
                "latitude": str(place.latitude) if place else None,
                "longitude": str(place.longitude) if place else None,
            })
        response_data = attach_activity_suggestions(
            response_data,
            trip,
            itinerary,
        )

    # Old plans have no snapshot; treat them as out of date too.
    response_data["stale"] = response_data.get("trip_snapshot") != trip_snapshot(trip)
    return Response(response_data, status=status.HTTP_200_OK)


# ---------------- ADD ITINERARY ITEM ----------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_itinerary_item(request, trip_id):
    with transaction.atomic():
        trip, plan = get_request_plan(
            request,
            trip_id,
            lock=True
        )

        if plan is None:
            return Response(
                {"error": "Regenerate this plan before editing it."},
                status=status.HTTP_409_CONFLICT,
            )

        body_error = json_object_error(request)
        if body_error:
            return body_error

        day = validate_trip_day(
            trip,
            request.data.get("day")
        )

        if day is None:
            return Response(
                {"error": "Invalid itinerary day for this trip."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        place_id = request.data.get("place_id")

        if place_id in (None, "", False):
            return Response(
                {"error": "place_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            place_id = int(place_id)
            if isinstance(request.data.get("place_id"), bool):
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"error": "place_id must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        destination = find_destination_record(trip.destination)
        place = get_object_or_404(
            Place,
            id=place_id,
            destination=destination,
        )

        if (place.category or "").lower() in NON_ITINERARY_CATEGORIES:
            return Response(
                {
                    "error": (
                        "Lodging and non-visit locations cannot be added "
                        "to the itinerary."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        day_items = list(
            ItineraryItem.objects.filter(
                plan=plan,
                day=day,
            ).select_related("place").order_by("order")
        )
        day_date = trip.start_date + timedelta(days=day - 1)

        requested_start = request.data.get("start_time")
        if requested_start:
            start_time = parse_time(str(requested_start))
        else:
            last_item = max(
                day_items,
                key=lambda item: item.end_time,
                default=None,
            )
            start_datetime = datetime.combine(
                day_date,
                last_item.end_time if last_item else time(9, 0),
            )
            if (
                last_item
                and last_item.place
                and last_item.place.latitude is not None
                and last_item.place.longitude is not None
            ):
                distance = haversine_coordinates(
                    last_item.place.latitude,
                    last_item.place.longitude,
                    place.latitude,
                    place.longitude,
                )
                travel_minutes = max(5, min(60, int((distance / 25) * 60)))
                start_datetime += timedelta(minutes=travel_minutes)

            lunch_start = datetime.combine(day_date, time(12, 30))
            lunch_end = datetime.combine(day_date, time(13, 15))
            visit_minutes = max(int(place.time_required or 60), 30)
            if lunch_start <= start_datetime < lunch_end:
                start_datetime = lunch_end
            elif (
                start_datetime < lunch_start
                and start_datetime + timedelta(minutes=visit_minutes) > lunch_start
            ):
                start_datetime = lunch_end
            if (
                start_datetime.date() != day_date
                or start_datetime + timedelta(minutes=visit_minutes)
                > datetime.combine(day_date + timedelta(days=1), time(0, 0))
            ):
                return Response(
                    {"error": PAST_MIDNIGHT_ERROR},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            start_time = start_datetime.time()

        if start_time is None:
            return Response(
                {"error": "start_time must use HH:MM format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        end_value = request.data.get("end_time")

        if end_value:
            end_time = parse_time(str(end_value))
        else:
            visit_minutes = max(int(place.time_required or 60), 30)
            end_datetime = (
                datetime.combine(day_date, start_time)
                + timedelta(minutes=visit_minutes)
            )
            if end_datetime.date() != day_date:
                return Response(
                    {"error": PAST_MIDNIGHT_ERROR},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            end_time = end_datetime.time()

        if end_time is None or end_time <= start_time:
            return Response(
                {
                    "error": (
                        "end_time must be later than start_time "
                        "and use HH:MM format."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order = int(
                request.data.get("order", len(day_items) + 1)
            )
        except (TypeError, ValueError):
            return Response(
                {"error": "order must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if order < 1 or order > len(day_items) + 1:
            return Response(
                {
                    "error": (
                        f"order must be between 1 and {len(day_items) + 1}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        for index, existing in enumerate(day_items, start=1):
            existing.order = 100000 + index

        if day_items:
            ItineraryItem.objects.bulk_update(day_items, ["order"])

        new_item = ItineraryItem.objects.create(
            plan=plan,
            day=day,
            place=place,
            start_time=start_time,
            end_time=end_time,
            order=1000000,
        )

        day_items.insert(order - 1, new_item)
        assign_day_orders(day_items, day)

        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Activity added successfully.",
            "added_item_id": new_item.id,
            "plan": response_data,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def dismiss_activity_suggestion(request, trip_id, place_id):
    with transaction.atomic():
        trip, plan = get_request_plan(
            request,
            trip_id,
            lock=True,
        )

        if plan is None:
            return Response(
                {"error": "Regenerate this plan before editing it."},
                status=status.HTTP_409_CONFLICT,
            )

        destination = find_destination_record(trip.destination)
        get_object_or_404(
            Place,
            id=place_id,
            destination=destination,
        )

        response_data = plan.response_data or {}
        dismissed_ids = {
            int(value)
            for value in response_data.get("dismissed_activity_ids", [])
            if str(value).isdigit()
        }
        dismissed_ids.add(place_id)
        response_data["dismissed_activity_ids"] = sorted(dismissed_ids)
        plan.response_data = response_data
        plan.save(update_fields=["response_data"])
        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Suggestion dismissed.",
            "plan": response_data,
        },
        status=status.HTTP_200_OK,
    )


# ---------------- DELETE ITINERARY ITEM ----------------

@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_itinerary_item(request, trip_id, item_id):
    with transaction.atomic():
        trip, plan = get_request_plan(
            request,
            trip_id,
            lock=True
        )

        if plan is None:
            return Response(
                {"error": "Regenerate this plan before editing it."},
                status=status.HTTP_409_CONFLICT,
            )

        item = get_object_or_404(
            ItineraryItem,
            id=item_id,
            plan=plan
        )

        affected_day = item.day
        if item.place_id:
            response_data = plan.response_data or {}
            dismissed_ids = {
                int(value)
                for value in response_data.get("dismissed_activity_ids", [])
                if str(value).isdigit()
            }
            dismissed_ids.add(item.place_id)
            response_data["dismissed_activity_ids"] = sorted(dismissed_ids)
            plan.response_data = response_data
            plan.save(update_fields=["response_data"])
        item.delete()

        remaining = list(
            ItineraryItem.objects.filter(
                plan=plan,
                day=affected_day
            ).order_by("order")
        )

        assign_day_orders(remaining, affected_day)
        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Activity removed successfully.",
            "plan": response_data
        },
        status=status.HTTP_200_OK,
    )


# ---------------- MOVE ITINERARY ITEM ----------------

@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def move_itinerary_item(request, trip_id, item_id):
    with transaction.atomic():
        trip, plan = get_request_plan(
            request,
            trip_id,
            lock=True
        )

        if plan is None:
            return Response(
                {"error": "Regenerate this plan before editing it."},
                status=status.HTTP_409_CONFLICT,
            )

        item = get_object_or_404(
            ItineraryItem,
            id=item_id,
            plan=plan
        )

        body_error = json_object_error(request)
        if body_error:
            return body_error

        new_day = validate_trip_day(
            trip,
            request.data.get("day")
        )

        if new_day is None:
            return Response(
                {"error": "Invalid destination day for this trip."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_day = item.day

        target_items = list(
            ItineraryItem.objects.filter(
                plan=plan,
                day=new_day
            )
            .exclude(id=item.id)
            .select_related("place")
            .order_by("order")
        )

        try:
            new_order = int(
                request.data.get("order", len(target_items) + 1)
            )
        except (TypeError, ValueError):
            return Response(
                {"error": "order must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if new_order < 1 or new_order > len(target_items) + 1:
            return Response(
                {
                    "error": (
                        f"order must be between 1 and {len(target_items) + 1}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_start = item.start_time
        new_end = item.end_time

        if "start_time" in request.data:
            new_start = parse_time(str(request.data["start_time"]))

        if "end_time" in request.data:
            new_end = parse_time(str(request.data["end_time"]))

        if new_start is None or new_end is None or new_end <= new_start:
            return Response(
                {"error": "Invalid activity time range."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item.day = new_day
        item.start_time = new_start
        item.end_time = new_end
        item.order = 1000000

        item.save(
            update_fields=[
                "day",
                "start_time",
                "end_time",
                "order"
            ]
        )

        if old_day != new_day:
            source_items = list(
                ItineraryItem.objects.filter(
                    plan=plan,
                    day=old_day
                ).order_by("order")
            )
            assign_day_orders(source_items, old_day)

        target_items.insert(new_order - 1, item)
        assign_day_orders(target_items, new_day)

        explicit_times = (
            "start_time" in request.data or "end_time" in request.data
        )
        if not explicit_times:
            # No times given: re-time the target day in its new order so
            # the moved activity does not overlap the others.
            for target in target_items:
                target.refresh_from_db(fields=["start_time", "end_time"])
            if not reflow_day_times(
                target_items,
                trip.start_date + timedelta(days=new_day - 1),
            ):
                transaction.set_rollback(True)
                return Response(
                    {"error": PAST_MIDNIGHT_ERROR},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Activity moved successfully.",
            "plan": response_data
        },
        status=status.HTTP_200_OK,
    )


# ---------------- REORDER ITINERARY ----------------

@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def reorder_itinerary(request, trip_id):
    with transaction.atomic():
        trip, plan = get_request_plan(
            request,
            trip_id,
            lock=True
        )

        if plan is None:
            return Response(
                {"error": "Regenerate this plan before editing it."},
                status=status.HTTP_409_CONFLICT,
            )

        body_error = json_object_error(request)
        if body_error:
            return body_error

        day = validate_trip_day(
            trip,
            request.data.get("day")
        )

        if day is None:
            return Response(
                {"error": "Invalid itinerary day."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item_ids = request.data.get("item_ids")

        if not isinstance(item_ids, list) or not item_ids:
            return Response(
                {"error": "item_ids must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            item_ids = [int(value) for value in item_ids]
        except (TypeError, ValueError):
            return Response(
                {"error": "All item_ids must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(item_ids) != len(set(item_ids)):
            return Response(
                {"error": "Duplicate item IDs are not allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        day_items = list(
            ItineraryItem.objects.filter(
                plan=plan,
                day=day
            ).select_related("place")
        )

        current_ids = {item.id for item in day_items}

        if set(item_ids) != current_ids:
            return Response(
                {
                    "error": (
                        "item_ids must contain every itinerary item "
                        "for this day exactly once."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        items_by_id = {
            item.id: item
            for item in day_items
        }

        reordered_items = [
            items_by_id[item_id]
            for item_id in item_ids
        ]

        if not reflow_day_times(
            reordered_items,
            trip.start_date + timedelta(days=day - 1),
        ):
            return Response(
                {"error": PAST_MIDNIGHT_ERROR},
                status=status.HTTP_400_BAD_REQUEST,
            )
        assign_day_orders(reordered_items, day)
        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Itinerary reordered successfully.",
            "plan": response_data
        },
        status=status.HTTP_200_OK,
    )
