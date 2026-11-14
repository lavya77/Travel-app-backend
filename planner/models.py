
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


class Plan(models.Model):

    PLAN_TYPES = [
        ('optimal', 'Optimal'),
    ]

    trip = models.ForeignKey(
        'trips.Trip',
        on_delete=models.CASCADE,
        related_name='plans'
    )

    plan_type = models.CharField(
        max_length=20,
        choices=PLAN_TYPES,
        default='optimal'
    )

    estimated_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0
    )

    estimated_effort = models.FloatField(default=0)

    # Stores the complete generated planner response for GET requests.
    response_data = models.JSONField(
        default=dict,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

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

    class Meta:
        ordering = ['day', 'order']

    def __str__(self):
        return f"Day {self.day} - {self.plan}"


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
