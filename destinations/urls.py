from rest_framework.routers import DefaultRouter

from .views import DestinationViewSet, PlaceViewSet

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

urlpatterns = router.urls