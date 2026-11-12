
from django.urls import path

from . import views


urlpatterns = [

    path(
        'trips/<int:trip_id>/generate/',
        views.generate_plan,
        name='generate-plan'
    ),

    path(
        'trips/<int:trip_id>/latest/',
        views.latest_plan,
        name='latest-plan'
    ),

]
