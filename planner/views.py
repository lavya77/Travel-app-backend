
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_time

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from trips.models import Trip
from destinations.models import Place

from .models import Plan, ItineraryItem
from .serializers import PlanSerializer
from .services import generate_optimal_plan, build_daily_timeline

logger = logging.getLogger(__name__)


NON_ITINERARY_CATEGORIES = {
    "restaurant",
    "cafe",
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

    for item in items:
        place = item.place
        old_data = previous_itinerary.get(item.id, {})

        if place:
            place_name = place.name
            category = place.category
            latitude = str(place.latitude)
            longitude = str(place.longitude)

            if place.estimated_cost > 0:
                attraction_cost += place.estimated_cost
            else:
                has_unknown_attraction_cost = True

            visit_minutes = max(int(place.time_required or 60), 30)
        else:
            place_name = None
            category = None
            latitude = None
            longitude = None
            visit_minutes = 60

        updated_itinerary.append({
            "id": item.id,
            "day": item.day,
            "place": place_name,
            "category": category,
            "start_time": item.start_time.strftime("%H:%M"),
            "end_time": item.end_time.strftime("%H:%M"),
            "order": item.order,
            "latitude": latitude,
            "longitude": longitude,
            "visit_minutes": old_data.get("visit_minutes", visit_minutes),
            "travel_minutes_before": old_data.get("travel_minutes_before", 0),
            "distance_before_km": old_data.get("distance_before_km", 0),
        })

    response_data["itinerary"] = updated_itinerary

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

    trip_data = response_data.get("trip", {})
    budget = safe_decimal(
        trip_data.get("budget", plan.trip.estimated_budget)
    )

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

    # Keep full-day timeline synchronized after add/remove/move/reorder.
    response_data["daily_schedule"] = build_daily_timeline(
        updated_itinerary,
        response_data.get("dining_recommendations", []),
        plan.trip,
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

    try:
        with transaction.atomic():
            result = generate_optimal_plan(trip, use_ai=use_ai)

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

    return Response(
        PlanSerializer(plan).data,
        status=status.HTTP_200_OK
    )


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

        if not place_id:
            return Response(
                {"error": "place_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        place = get_object_or_404(
            Place,
            id=place_id,
            destination__name__iexact=trip.destination,
        )

        if (place.category or "").lower() in NON_ITINERARY_CATEGORIES:
            return Response(
                {
                    "error": (
                        "Restaurants, cafes and non-visit locations belong "
                        "in recommendations, not the sightseeing itinerary."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_time = parse_time(
            str(request.data.get("start_time", "09:00"))
        )

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
            end_time = (
                datetime.combine(trip.start_date, start_time)
                + timedelta(minutes=visit_minutes)
            ).time()

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

        day_items = list(
            ItineraryItem.objects.filter(
                plan=plan,
                day=day
            ).order_by("order")
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
            )
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

        assign_day_orders(reordered_items, day)
        response_data = refresh_plan_response(plan)

    return Response(
        {
            "message": "Itinerary reordered successfully.",
            "plan": response_data
        },
        status=status.HTTP_200_OK,
    )
