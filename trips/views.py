from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Trip, TransportOption, Accommodation
from .serializers import (
    TripSerializer,
    TransportOptionSerializer,
    AccommodationSerializer
)


class TripViewSet(viewsets.ModelViewSet):
    serializer_class = TripSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Trip.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class TransportOptionViewSet(viewsets.ModelViewSet):
    serializer_class = TransportOptionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TransportOption.objects.filter(
            trip__user=self.request.user
        )


class AccommodationViewSet(viewsets.ModelViewSet):
    serializer_class = AccommodationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Accommodation.objects.filter(
            trip__user=self.request.user
        )