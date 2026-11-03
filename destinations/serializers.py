from rest_framework import serializers
from .models import Destination, Place


class PlaceSerializer(serializers.ModelSerializer):

    class Meta:
        model = Place
        fields = '__all__'


class DestinationSerializer(serializers.ModelSerializer):

    places = PlaceSerializer(many=True, read_only=True)

    class Meta:
        model = Destination
        fields = '__all__'