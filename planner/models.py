
from django.db import models


class Plan(models.Model):

    PLAN_TYPES = [
        ('economy', 'Economy'),
        ('balanced', 'Balanced'),
        ('convenience', 'Convenience'),
    ]

    trip = models.ForeignKey(
        'trips.Trip',
        on_delete=models.CASCADE,
        related_name='plans'
    )

    plan_type = models.CharField(
        max_length=20,
        choices=PLAN_TYPES
    )

    estimated_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    estimated_effort = models.FloatField(
        default=0
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.trip} - {self.plan_type}"


class ItineraryItem(models.Model):

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name='itinerary_items'
    )

    day = models.PositiveIntegerField()

    place = models.ForeignKey(
        'destinations.Place',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='itinerary_items'
    )

    start_time = models.TimeField()

    end_time = models.TimeField()

    order = models.PositiveIntegerField()

    def __str__(self):
        return f"Day {self.day} - {self.plan}"
    
    class Meta:
        ordering = ['day', 'order']

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator


class Feedback(models.Model):

    trip = models.ForeignKey(
        'trips.Trip',
        on_delete=models.CASCADE,
        related_name='feedback'
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='feedback'
    )

    rating = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(5)
        ]
    )

    comments = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Feedback for {self.trip}"
