
from django.contrib import admin
from django.urls import path, include

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from config.throttles import AuthThrottle
from integrations.views import flight_search, hotel_search


urlpatterns = [
    path('admin/', admin.site.urls),

    # Existing APIs
    path('api/', include('trips.urls')),
    path('api/', include('destinations.urls')),
    path('api/accounts/', include('accounts.urls')),

    # Flight and hotel search APIs
    path('api/flights/search/', flight_search, name='flight-search'),
    path('api/hotels/search/', hotel_search, name='hotel-search'),

    # JWT authentication
    path(
        'api/token/',
        TokenObtainPairView.as_view(throttle_classes=[AuthThrottle]),
        name='token_obtain_pair'
    ),
    path(
        'api/token/refresh/',
        TokenRefreshView.as_view(),
        name='token_refresh'
    ),

    path('api/planner/', include('planner.urls')),
]
