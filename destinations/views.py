
from rest_framework import viewsets, status
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .models import Destination, Place
from .serializers import DestinationSerializer, PlaceSerializer

from integrations.save_places import fetch_and_save_city_places


class DestinationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Destination.objects.all()
    serializer_class = DestinationSerializer
    permission_classes = [AllowAny]


class PlaceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    permission_classes = [AllowAny]


@api_view(['POST'])
@permission_classes([AllowAny])
def search_destination(request):

    city = request.data.get("city")
    state = request.data.get("state")

    if not city or not state:
        return Response(
            {"error": "City and state are required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        result = fetch_and_save_city_places(city, state)

        if result.get("total_places_received", 0) == 0:
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        destination = Destination.objects.get(name=city)

        places = Place.objects.filter(destination=destination)

        return Response({
            "message": "Places fetched and saved successfully.",
            "result": result,
            "places": PlaceSerializer(places, many=True).data
        }, status=status.HTTP_200_OK)

    except Exception as error:
        return Response(
            {"error": str(error)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
