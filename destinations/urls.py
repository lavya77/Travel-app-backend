
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DestinationViewSet,
    PlaceViewSet,
    city_suggestions,
    search_destination
)

router = DefaultRouter()

router.register(
    'destinations',
    DestinationViewSet,
    basename='destination'
)

router.register(
    'places',
    PlaceViewSet,
    basename='place'
)

urlpatterns = [
    path(
        'destinations/suggestions/',
        city_suggestions,
        name='city-suggestions'
    ),
    path(
        'destinations/search/',
        search_destination,
        name='search-destination'
    ),
]

urlpatterns += router.urls
