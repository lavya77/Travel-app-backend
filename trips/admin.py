from django.contrib import admin
from .models import Trip, TransportOption, Accommodation


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = (
        'source',
        'destination',
        'user',
        'start_date',
        'end_date',
        'estimated_budget',
    )

    list_filter = ('travel_mode', 'start_date')
    search_fields = ('source', 'destination')


admin.site.register(TransportOption)
admin.site.register(Accommodation)