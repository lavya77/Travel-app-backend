
import hashlib
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from config.throttles import ExternalApiThrottle

from .models import Destination, Place, Hotel
from .serializers import (
    DestinationSerializer,
    PlaceSerializer,
    HotelSerializer,
)

from integrations.save_places import (
    fetch_and_save_city_places,
    fetch_and_save_city_hotels,
)
from integrations.geocoding import search_city_suggestions


logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([AnonRateThrottle, UserRateThrottle])
def city_suggestions(request):
    query = request.query_params.get("q", "").strip()
    if len(query) < 2:
        return Response({"suggestions": []})
    if len(query) > 100:
        return Response(
            {"error": "City search must be 100 characters or fewer."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    api_key = settings.GEOAPIFY_API_KEY.strip()
    if not api_key:
        return Response(
            {"error": "City autocomplete is not configured on the server."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    cache_key = "geoapify-city-suggestions:" + hashlib.sha256(
        query.casefold().encode("utf-8")
    ).hexdigest()
    suggestions = cache.get(cache_key)
    if suggestions is not None:
        return Response({"suggestions": suggestions})

    try:
        suggestions = search_city_suggestions(query, api_key)
    except (requests.RequestException, ValueError) as error:
        logger.warning(
            "Geoapify city suggestions failed (%s)",
            type(error).__name__,
        )
        return Response(
            {"error": "City suggestions are temporarily unavailable."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    cache.set(cache_key, suggestions, timeout=3600)
    return Response({"suggestions": suggestions})


class DestinationViewSet(viewsets.ReadOnlyModelViewSet):

    queryset = Destination.objects.all()
    serializer_class = DestinationSerializer
    permission_classes = [AllowAny]


class PlaceViewSet(viewsets.ReadOnlyModelViewSet):

    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    permission_classes = [AllowAny]


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ExternalApiThrottle])
def search_destination(request):

    if not isinstance(request.data, dict):
        return Response(
            {"error": "The request body must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST
        )

    city = request.data.get("city", "")
    state = request.data.get("state", "")
    if not isinstance(city, str) or not isinstance(state, str):
        return Response(
            {"error": "City and state must be text."},
            status=status.HTTP_400_BAD_REQUEST
        )
    city = city.strip()
    state = state.strip()

    if not city or not state:
        return Response(
            {"error": "City and state are required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        result = fetch_and_save_city_places(city, state)

        if result.get("total_places_received", 0) == 0:
            return Response(
                result,
                status=status.HTTP_404_NOT_FOUND
            )

        # Case-insensitive lookup.
        destination = (
            Destination.objects
            .filter(name__iexact=city)
            .order_by("id")
            .first()
        )

        if not destination:
            return Response(
                {"error": "Destination could not be found after saving."},
                status=status.HTTP_404_NOT_FOUND
            )

        places = Place.objects.filter(
            destination=destination
        )

        # Also fetch and save hotels so hotel search and the planner's
        # hotel recommendations work for newly discovered cities.
        # Places are already saved, so a hotel failure must not fail
        # the whole request.
        try:
            hotels_result = fetch_and_save_city_hotels(city, state)
        except Exception:
            logger.exception("Hotel discovery failed for %s", city)
            hotels_result = {
                "destination": city,
                "new_hotels_saved": 0,
                "total_hotels_received": 0,
                "error": (
                    "Hotels could not be fetched right now. "
                    "Places were saved; try again later."
                ),
            }

        hotels = Hotel.objects.filter(
            destination=destination
        ).order_by("name")

        return Response(
            {
                "message": "Places and hotels fetched and saved successfully.",
                "result": result,
                "hotels_result": hotels_result,
                "places": PlaceSerializer(
                    places,
                    many=True
                ).data,
                "hotels": HotelSerializer(
                    hotels,
                    many=True
                ).data,
            },
            status=status.HTTP_200_OK
        )

    except Exception:
        logger.exception("Destination discovery failed for %s", city)
        return Response(
            {"error": "Unable to search and save destination places."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
