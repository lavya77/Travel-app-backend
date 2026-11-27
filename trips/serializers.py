
from decimal import Decimal

from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework.exceptions import ValidationError

from integrations.geocoding import same_city

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

    MAX_TRIP_DAYS = 30

    def validate(self, attrs):
        def current(field):
            return attrs.get(field, getattr(self.instance, field, None))

        start_date = current('start_date')
        end_date = current('end_date')
        errors = {}

        if start_date and end_date:
            if end_date < start_date:
                errors['end_date'] = "End date cannot be before start date."
            elif (end_date - start_date).days + 1 > self.MAX_TRIP_DAYS:
                errors['end_date'] = (
                    f"Trips can be at most {self.MAX_TRIP_DAYS} days long."
                )

        people = current('number_of_people')
        if people is not None and people < 1:
            errors['number_of_people'] = "At least one traveller is required."

        budget = current('estimated_budget')
        if budget is not None and budget < 0:
            errors['estimated_budget'] = "Budget cannot be negative."

        if errors:
            raise ValidationError(errors)
        return attrs


class TransportOptionSerializer(serializers.ModelSerializer):

    class Meta:
        model = TransportOption
        fields = '__all__'
        read_only_fields = ['is_estimate']

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
        read_only_fields = ['is_estimate']

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
            if not same_city(hotel.destination.name, trip.destination):
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

            if hotel.website and 'booking_url' not in attrs:
                try:
                    URLValidator(schemes=['http', 'https'])(hotel.website)
                except DjangoValidationError:
                    pass
                else:
                    attrs['booking_url'] = hotel.website

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
