
from django.db import models


class Destination(models.Model):

    name = models.CharField(max_length=150, unique=True)

    state = models.CharField(max_length=100)

    country = models.CharField(max_length=100, default="India")

    description = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}, {self.state}"


class Place(models.Model):

    CATEGORY_CHOICES = [
        ('attraction', 'Attraction'),
        ('viewpoint', 'Viewpoint'),
        ('cafe', 'Cafe'),
        ('restaurant', 'Restaurant'),
        ('shopping', 'Shopping'),
        ('trek', 'Trek'),
        ('other', 'Other'),
    ]

    destination = models.ForeignKey(
        Destination,
        on_delete=models.CASCADE,
        related_name='places'
    )

    name = models.CharField(max_length=200)

    # Unique OpenStreetMap ID to prevent duplicate places
    osm_id = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True
    )

    category = models.CharField(
        max_length=30,
        choices=CATEGORY_CHOICES
    )

    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7
    )

    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7
    )

    time_required = models.PositiveIntegerField(
        help_text="Estimated visiting time in minutes"
    )

    estimated_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Hotel(models.Model):

    ACCOMMODATION_TYPES = [
        ('hotel', 'Hotel'),
        ('hostel', 'Hostel'),
        ('guest_house', 'Guest House'),
        ('motel', 'Motel'),
        ('apartment', 'Apartment'),
        ('chalet', 'Chalet'),
    ]

    destination = models.ForeignKey(
        Destination,
        on_delete=models.CASCADE,
        related_name='hotels'
    )

    name = models.CharField(max_length=200)

    osm_id = models.CharField(max_length=50)

    osm_type = models.CharField(max_length=20)

    category = models.CharField(
        max_length=30,
        choices=ACCOMMODATION_TYPES
    )

    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7
    )

    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7
    )

    street = models.CharField(max_length=255, blank=True)

    city = models.CharField(max_length=150, blank=True)

    postcode = models.CharField(max_length=20, blank=True)

    website = models.URLField(blank=True)

    phone = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['osm_id', 'osm_type'],
                name='unique_osm_hotel'
            )
        ]

    def __str__(self):
        return self.name
