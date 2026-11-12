
from decimal import Decimal

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import Trip, TransportOption, Accommodation
from destinations.models import Hotel


class TripSerializer(serializers.ModelSerializer):

    class Meta:
        model = Trip
        fields = '__all__'

        read_only_fields = [
            'user',
            'created_at',
            'updated_at',
        ]


class TransportOptionSerializer(serializers.ModelSerializer):

    class Meta:
        model = TransportOption
        fields = '__all__'

    def validate(self, attrs):

        trip = attrs.get(
            'trip',
            getattr(self.instance, 'trip', None)
        )

        request = self.context.get('request')

        if (
            trip
            and request
            and trip.user_id != request.user.id
        ):
            raise ValidationError(
                "You can only add transport to your own trips."
            )

        return attrs


class AccommodationSerializer(serializers.ModelSerializer):

    hotel = serializers.PrimaryKeyRelatedField(
        queryset=Hotel.objects.all(),
        required=False,
        allow_null=True
    )

    name = serializers.CharField(required=False)

    location = serializers.CharField(required=False)

    accommodation_type = serializers.ChoiceField(
        choices=Accommodation.ACCOMMODATION_TYPES,
        required=False
    )

    price_per_night = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('0.01')
    )

    class Meta:
        model = Accommodation

        fields = '__all__'

    def validate(self, attrs):

        trip = attrs.get(
            'trip',
            getattr(self.instance, 'trip', None)
        )

        hotel = attrs.get(
            'hotel',
            getattr(self.instance, 'hotel', None)
        )

        request = self.context.get('request')

        # Ensure the trip belongs to the logged-in user.
        if (
            trip
            and request
            and trip.user_id != request.user.id
        ):
            raise ValidationError(
                "You can only add accommodation to your own trips."
            )

        if hotel:

            if not trip:
                raise ValidationError(
                    {"trip": "A trip is required when selecting a hotel."}
                )

            # Ensure the hotel belongs to the trip's destination.
            if (
                hotel.destination.name.strip().lower()
                != trip.destination.strip().lower()
            ):
                raise ValidationError(
                    {
                        "hotel": (
                            "This hotel does not belong to the "
                            "selected trip destination."
                        )
                    }
                )

            attrs.setdefault('name', hotel.name)

            address_parts = [
                hotel.street,
                hotel.city,
                hotel.postcode,
            ]

            hotel_location = ', '.join(
                part for part in address_parts if part
            )

            attrs.setdefault(
                'location',
                hotel_location or trip.destination
            )

            # Map OSM categories into our existing choices.
            category_mapping = {
                'hotel': 'hotel',
                'hostel': 'hostel',
                'guest_house': 'guesthouse',
                'motel': 'hotel',
                'apartment': 'homestay',
                'chalet': 'resort',
            }

            mapped_type = category_mapping.get(
                hotel.category,
                'hotel'
            )

            attrs.setdefault(
                'accommodation_type',
                mapped_type
            )

            attrs.setdefault(
                'booking_url',
                hotel.website
            )

        # Require these fields when no saved hotel is selected.
        if not attrs.get(
            'name',
            getattr(self.instance, 'name', None)
        ):
            raise ValidationError(
                {"name": "Accommodation name is required."}
            )

        if not attrs.get(
            'location',
            getattr(self.instance, 'location', None)
        ):
            raise ValidationError(
                {"location": "Accommodation location is required."}
            )

        if not attrs.get(
            'accommodation_type',
            getattr(self.instance, 'accommodation_type', None)
        ):
            raise ValidationError(
                {"accommodation_type": "Accommodation type is required."}
            )

        return attrs
