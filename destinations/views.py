
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

import logging

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


logger = logging.getLogger(__name__)


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
def search_destination(request):

    city = request.data.get("city", "").strip()
    state = request.data.get("state", "").strip()

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
