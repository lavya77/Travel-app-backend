
from rest_framework import serializers

from .models import Plan, ItineraryItem, Feedback
from destinations.serializers import PlaceSerializer


class ItineraryItemSerializer(serializers.ModelSerializer):

    place_details = PlaceSerializer(
        source='place',
        read_only=True
    )

    class Meta:
        model = ItineraryItem

        fields = [
            'id',
            'day',
            'place',
            'place_details',
            'start_time',
            'end_time',
            'order',
        ]


class PlanSerializer(serializers.ModelSerializer):

    itinerary_items = ItineraryItemSerializer(
        many=True,
        read_only=True
    )

    response_data = serializers.JSONField(
        read_only=True
    )

    class Meta:
        model = Plan

        fields = [
            'id',
            'trip',
            'plan_type',
            'estimated_cost',
            'estimated_effort',
            'created_at',
            'itinerary_items',
            'response_data',
        ]

    def to_representation(self, instance):
        """
        Return the complete saved planner response.

        This makes the Latest Plan endpoint return the same
        JSON structure as the Generate Plan endpoint.
        """

        if instance.response_data:
            return instance.response_data

        return super().to_representation(instance)


class FeedbackSerializer(serializers.ModelSerializer):

    class Meta:
        model = Feedback

        fields = '__all__'

        read_only_fields = [
            'user',
            'created_at',
        ]
