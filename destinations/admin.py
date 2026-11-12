
from django.contrib import admin
from .models import Destination, Place, Hotel


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


@admin.register(Hotel)
class HotelAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'destination',
        'category',
        'city',
        'phone',
    )

    list_filter = ('category', 'destination')
    search_fields = ('name', 'city', 'street')
    readonly_fields = ('created_at', 'updated_at')
