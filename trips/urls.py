from rest_framework.routers import DefaultRouter

from .views import (
    TripViewSet,
    TransportOptionViewSet,
    AccommodationViewSet
)

router = DefaultRouter()

router.register(
    r'trips',
    TripViewSet,
    basename='trip'
)

router.register(
    r'transport-options',
    TransportOptionViewSet,
    basename='transport-option'
)

router.register(
    r'accommodations',
    AccommodationViewSet,
    basename='accommodation'
)

urlpatterns = router.urls