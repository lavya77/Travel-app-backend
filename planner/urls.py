
from django.urls import path

from . import views


urlpatterns = [

    # Generate an optimal travel plan
    path(
        'trips/<int:trip_id>/generate/',
        views.generate_plan,
        name='generate-plan'
    ),

    # Retrieve the latest saved plan
    path(
        'trips/<int:trip_id>/latest/',
        views.latest_plan,
        name='latest-plan'
    ),

    # Add a place to the itinerary
    path(
        'trips/<int:trip_id>/itinerary/items/',
        views.add_itinerary_item,
        name='add-itinerary-item'
    ),

    # Dismiss an optional activity suggestion
    path(
        'trips/<int:trip_id>/activity-suggestions/<int:place_id>/dismiss/',
        views.dismiss_activity_suggestion,
        name='dismiss-activity-suggestion'
    ),

    # Remove an itinerary activity
    path(
        'trips/<int:trip_id>/itinerary/items/<int:item_id>/',
        views.delete_itinerary_item,
        name='delete-itinerary-item'
    ),

    # Move an activity to another day
    path(
        'trips/<int:trip_id>/itinerary/items/<int:item_id>/move/',
        views.move_itinerary_item,
        name='move-itinerary-item'
    ),

    # Reorder activities
    path(
        'trips/<int:trip_id>/itinerary/reorder/',
        views.reorder_itinerary,
        name='reorder-itinerary'
    ),
]
