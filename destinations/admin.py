from django.contrib import admin
from .models import Destination, Place


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    list_display = ('name', 'state', 'country')
    search_fields = ('name', 'state')


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'destination',
        'category',
        'estimated_cost',
        'time_required',
    )

    list_filter = ('category', 'destination')
    search_fields = ('name',)