from django.conf import settings
from django.db import models


class Trip(models.Model):

    TRAVEL_MODE_CHOICES = [
        ('self_vehicle', 'Self Vehicle'),
        ('rented_vehicle', 'Rented Vehicle'),
        ('public_transport', 'Public Transport'),
        ('any', 'Any Mode'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trips'
    )

    source = models.CharField(max_length=150)

    destination = models.CharField(max_length=150)

    start_date = models.DateField()

    end_date = models.DateField()

    number_of_people = models.PositiveIntegerField(default=1)

    estimated_budget = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    travel_mode = models.CharField(
        max_length=30,
        choices=TRAVEL_MODE_CHOICES,
        default='any'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.source} to {self.destination}"
    
class TransportOption(models.Model):

    TRANSPORT_TYPES = [
        ('flight', 'Flight'),
        ('train', 'Train'),
        ('bus', 'Bus'),
        ('car', 'Car'),
        ('bike', 'Bike'),
    ]

    trip = models.ForeignKey(
        Trip,
        on_delete=models.CASCADE,
        related_name='transport_options'
    )

    transport_type = models.CharField(
        max_length=20,
        choices=TRANSPORT_TYPES
    )

    origin = models.CharField(max_length=150)

    destination = models.CharField(max_length=150)

    departure_time = models.DateTimeField()

    arrival_time = models.DateTimeField()

    estimated_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    provider = models.CharField(
        max_length=150,
        blank=True
    )

    booking_url = models.URLField(blank=True)

    def __str__(self):
        return f"{self.transport_type}: {self.origin} to {self.destination}"



class Accommodation(models.Model):

    ACCOMMODATION_TYPES = [
        ('hotel', 'Hotel'),
        ('hostel', 'Hostel'),
        ('resort', 'Resort'),
        ('homestay', 'Homestay'),
        ('guesthouse', 'Guesthouse'),
    ]

    trip = models.ForeignKey(
        Trip,
        on_delete=models.CASCADE,
        related_name='accommodations'
    )

    name = models.CharField(max_length=200)

    accommodation_type = models.CharField(
        max_length=30,
        choices=ACCOMMODATION_TYPES
    )

    location = models.CharField(max_length=255)

    price_per_night = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True
    )

    booking_url = models.URLField(blank=True)

    def __str__(self):
        return self.name
