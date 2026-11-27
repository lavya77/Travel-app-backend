from django.db import transaction

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
        return Trip.objects.filter(
            user=self.request.user
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


def save_with_single_selection(serializer, siblings_filter, quote_fields):
    """Save an option; keep one selected per group; own quotes stop being estimates."""
    extra = {}
    instance = serializer.instance
    if instance is not None and instance.is_estimate and any(
        field in serializer.validated_data for field in quote_fields
    ):
        extra['is_estimate'] = False
    if instance is None:
        extra['is_estimate'] = False

    with transaction.atomic():
        option = serializer.save(**extra)
        if option.is_selected:
            (
                type(option).objects
                .filter(trip=option.trip, **siblings_filter(option))
                .exclude(pk=option.pk)
                .update(is_selected=False)
            )
    return option


class TransportOptionViewSet(viewsets.ModelViewSet):
    serializer_class = TransportOptionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            TransportOption.objects
            .filter(trip__user=self.request.user)
            .select_related('trip')
        )

    QUOTE_FIELDS = ('estimated_cost', 'departure_time', 'arrival_time', 'provider')

    def perform_create(self, serializer):
        save_with_single_selection(
            serializer,
            lambda option: {'direction': option.direction},
            self.QUOTE_FIELDS,
        )

    def perform_update(self, serializer):
        self.perform_create(serializer)


class AccommodationViewSet(viewsets.ModelViewSet):
    serializer_class = AccommodationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Accommodation.objects
            .filter(trip__user=self.request.user)
            .select_related('trip', 'hotel', 'hotel__destination')
        )

    QUOTE_FIELDS = ('price_per_night', 'rating')

    def perform_create(self, serializer):
        save_with_single_selection(
            serializer,
            lambda option: {},
            self.QUOTE_FIELDS,
        )

    def perform_update(self, serializer):
        self.perform_create(serializer)
