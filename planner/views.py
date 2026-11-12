
import logging

from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from trips.models import Trip
from .models import Plan
from .serializers import PlanSerializer
from .services import generate_optimal_plan


logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_plan(request, trip_id):

    trip = get_object_or_404(
        Trip,
        id=trip_id,
        user=request.user
    )

    if trip.end_date < trip.start_date:
        return Response(
            {'error': 'Trip end date cannot be before start date.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if trip.number_of_people < 1:
        return Response(
            {'error': 'At least one traveller is required.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        result = generate_optimal_plan(trip)

        return Response(
            result,
            status=status.HTTP_201_CREATED
        )

    except ValueError as error:

        return Response(
            {'error': str(error)},
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:

        logger.exception(
            "Optimal plan generation failed for trip %s",
            trip_id
        )

        return Response(
            {'error': 'Unable to generate the travel plan.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def latest_plan(request, trip_id):

    trip = get_object_or_404(
        Trip,
        id=trip_id,
        user=request.user
    )

    plan = (
        Plan.objects.filter(
            trip=trip,
            plan_type='optimal'
        )
        .prefetch_related('itinerary_items__place')
        .order_by('-created_at')
        .first()
    )

    if not plan:
        return Response(
            {'error': 'No generated plan found for this trip.'},
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = PlanSerializer(plan)

    return Response(
        serializer.data,
        status=status.HTTP_200_OK
    )
