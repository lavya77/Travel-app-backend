
from django.contrib import admin

from .models import Plan, ItineraryItem, Feedback


class ItineraryItemInline(admin.TabularInline):

    model = ItineraryItem
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):

    list_display = (
        'id',
        'trip',
        'plan_type',
        'estimated_cost',
        'estimated_effort',
        'created_at',
    )

    list_filter = (
        'plan_type',
        'created_at',
    )

    search_fields = (
        'trip__source',
        'trip__destination',
    )

    inlines = [ItineraryItemInline]


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):

    list_display = (
        'trip',
        'user',
        'rating',
        'created_at',
    )

    list_filter = ('rating',)

    search_fields = (
        'trip__destination',
        'user__username',
    )
