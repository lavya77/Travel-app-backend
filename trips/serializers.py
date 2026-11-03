from rest_framework import serializers
from .models import Trip, TransportOption, Accommodation


class TripSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trip
        fields = '__all__'
        read_only_fields = ['user', 'created_at', 'updated_at']


class TransportOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransportOption
        fields = '__all__'


class AccommodationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Accommodation
        fields = '__all__'