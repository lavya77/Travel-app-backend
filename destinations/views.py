from rest_framework import viewsets
from rest_framework.permissions import AllowAny

from .models import Destination, Place
from .serializers import DestinationSerializer, PlaceSerializer


class DestinationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Destination.objects.all()
    serializer_class = DestinationSerializer
    permission_classes = [AllowAny]


class PlaceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    permission_classes = [AllowAny]