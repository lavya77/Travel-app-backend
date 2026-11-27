from datetime import date
from urllib.parse import urlencode

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from destinations.models import Hotel
from integrations.save_places import find_destination_record
from trips.models import Trip


def _trip_for_search(request, params):
    trip_id = params.get("trip_id")
    if trip_id is None or trip_id == "":
        return None, None
    if isinstance(trip_id, bool) or not str(trip_id).isdecimal() or int(trip_id) < 1:
        return None, Response(
            {"error": "Trip ID must be a positive integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return get_object_or_404(Trip, pk=trip_id, user=request.user), None


def _request_params(request):
    return request.data if request.method == "POST" else request.query_params


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def flight_search(request):
    params = _request_params(request)
    origin = str(params.get("origin") or "").strip().upper()
    destination = str(params.get("destination") or "").strip().upper()
    date_from = str(params.get("date_from") or "").strip()
    adults = params.get("adults", "1")

    if not origin or not destination or not date_from:
        return Response(
            {"error": "Origin, destination and date are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (
        len(origin) != 3 or not origin.isalpha()
        or len(destination) != 3 or not destination.isalpha()
    ):
        return Response(
            {"error": "Use valid three-letter airport codes, e.g. DEL and BOM."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if origin == destination:
        return Response(
            {"error": "Origin and destination cannot be the same."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        travel_date = date.fromisoformat(date_from)
        if travel_date < date.today():
            raise ValueError
    except ValueError:
        return Response(
            {"error": "Enter a valid current or future date in YYYY-MM-DD format."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        if isinstance(adults, bool):
            raise ValueError
        adults = int(adults)
        if not 1 <= adults <= 9:
            raise ValueError
    except (TypeError, ValueError):
        return Response(
            {"error": "Adults must be between 1 and 9."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    _, error = _trip_for_search(request, params)
    if error:
        return error

    query = f"Flights from {origin} to {destination} on {date_from}"
    search_url = (
        "https://www.google.com/travel/flights?"
        f"{urlencode({'q': query, 'curr': 'INR', 'adults': adults})}"
    )
    return Response(
        {
            "source": "Google Flights",
            "search_url": search_url,
            "live_offers_in_app": False,
            "message": (
                "This free search link opens Google Flights. Prices and "
                "availability are shown there; this app does not fetch or "
                "rank live fares."
            ),
            "trip_search_scope": "outbound_one_way",
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def hotel_search(request):
    params = _request_params(request)
    city_name = str(params.get("city_name") or "").strip()
    if not city_name:
        return Response(
            {"error": "City name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    trip, error = _trip_for_search(request, params)
    if error:
        return error

    destination = find_destination_record(city_name)
    hotels = (
        Hotel.objects.filter(destination=destination).order_by("name")
        if destination else Hotel.objects.none()
    )
    hotel_data = [{
        "id": hotel.id,
        "name": hotel.name,
        "category": hotel.category,
        "osm_id": hotel.osm_id,
        "osm_type": hotel.osm_type,
        "latitude": hotel.latitude,
        "longitude": hotel.longitude,
        "address": {
            "street": hotel.street,
            "city": hotel.city,
            "postcode": hotel.postcode,
        },
        "website": hotel.website,
        "phone": hotel.phone,
        "source": "OpenStreetMap",
    } for hotel in hotels]

    search_terms = f"hotels in {city_name}"
    if trip:
        search_terms += f" {trip.start_date} to {trip.end_date}"
        search_terms += f" for {trip.number_of_people} guests"
    return Response(
        {
            "destination": city_name,
            "total_hotels": len(hotel_data),
            "source": "OpenStreetMap",
            "live_search_available": False,
            "search_url": (
                "https://www.google.com/search?"
                f"{urlencode({'q': search_terms})}"
            ),
            "hotels": hotel_data,
            "message": (
                "These free OpenStreetMap listings do not include room "
                "availability or prices. Use the external search link to "
                "check current offers."
            ),
        },
        status=status.HTTP_200_OK,
    )
