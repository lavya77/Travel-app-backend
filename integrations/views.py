
import logging
from datetime import date

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from integrations.flights import search_flights
from destinations.models import Hotel


logger = logging.getLogger(__name__)


# ---------------- FLIGHT SEARCH ----------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def flight_search(request):

    origin = request.query_params.get("origin", "").upper()
    destination = request.query_params.get("destination", "").upper()
    date_from = request.query_params.get("date_from", "")
    adults = request.query_params.get("adults", "1")

    if not origin or not destination or not date_from:
        return Response(
            {"error": "Origin, destination and date are required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    if (
        len(origin) != 3 or not origin.isalpha()
        or len(destination) != 3 or not destination.isalpha()
    ):
        return Response(
            {"error": "Use valid three-letter airport codes, e.g. DEL and BOM."},
            status=status.HTTP_400_BAD_REQUEST
        )

    if origin == destination:
        return Response(
            {"error": "Origin and destination cannot be the same."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        travel_date = date.fromisoformat(date_from)

        if travel_date < date.today():
            raise ValueError

    except ValueError:
        return Response(
            {"error": "Enter a valid current or future date in YYYY-MM-DD format."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        adults = int(adults)

        if not 1 <= adults <= 9:
            raise ValueError

    except ValueError:
        return Response(
            {"error": "Adults must be between 1 and 9."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        result = search_flights(
            origin=origin,
            destination=destination,
            date_from=date_from,
            adults=adults,
            currency="INR",
            limit=20,
            sort="price"
        )

        return Response(result, status=status.HTTP_200_OK)

    except Exception:
        logger.exception("Flight provider search failed")

        return Response(
            {"error": "Flight provider is temporarily unavailable. Please try again."},
            status=status.HTTP_502_BAD_GATEWAY
        )


# ---------------- HOTEL SEARCH ----------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def hotel_search(request):

    city_name = request.query_params.get("city_name", "").strip()

    if not city_name:
        return Response(
            {"error": "City name is required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        hotels = Hotel.objects.filter(
            destination__name__iexact=city_name
        ).order_by("name")

        hotel_data = []

        for hotel in hotels:
            hotel_data.append({
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
                    "postcode": hotel.postcode
                },
                "website": hotel.website,
                "phone": hotel.phone
            })

        return Response(
            {
                "destination": city_name,
                "total_hotels": len(hotel_data),
                "source": "OpenStreetMap",
                "hotels": hotel_data,
                "message": (
                    "Hotel discovery results. Live room availability "
                    "and prices are not currently provided."
                )
            },
            status=status.HTTP_200_OK
        )

    except Exception:
        logger.exception("Hotel database search failed")

        return Response(
            {"error": "Unable to retrieve hotels. Please try again."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
