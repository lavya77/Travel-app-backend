
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
