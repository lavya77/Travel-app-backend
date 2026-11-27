import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You assign saved sightseeing places to the days of a trip.
Use only the supplied place IDs. Include every supplied ID exactly once and do
not invent, alter, or omit IDs. Use every trip day exactly once, with an empty
place_ids list when appropriate. Consider the trip dates, budget, travel mode,
and supplied place details. Keep nearby places together. Fill each day with
about 5-6 hours of visits (the sum of visit_minutes) before starting the next
day; when places run out, leave the remaining later days empty rather than
spreading one or two places per day. Do not search externally or invent prices. Return only a JSON object
in this shape:
{"days":[{"day":1,"place_ids":[12,34]}]}"""

class AIResponseError(ValueError):
    pass


def validate_ai_schedule(data, place_ids, total_days):
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise AIResponseError("AI response must contain a days list.")

    allowed_ids = set(place_ids)
    parsed_days = {}
    seen_ids = set()

    for day_data in data["days"]:
        if not isinstance(day_data, dict):
            raise AIResponseError("AI response contains an invalid day.")

        raw_day = day_data.get("day")
        if isinstance(raw_day, bool):
            raise AIResponseError("AI response contains an invalid day number.")
        if isinstance(raw_day, int):
            day = raw_day
        elif isinstance(raw_day, str) and raw_day.isdecimal():
            day = int(raw_day)
        else:
            raise AIResponseError("AI response contains an invalid day number.")

        if day < 1 or day > total_days or day in parsed_days:
            raise AIResponseError("AI response contains an out-of-range or duplicate day.")

        raw_place_ids = day_data.get("place_ids")
        if not isinstance(raw_place_ids, list):
            raise AIResponseError("Each AI day must contain a place_ids list.")

        parsed_place_ids = []
        for raw_id in raw_place_ids:
            if isinstance(raw_id, bool):
                raise AIResponseError("AI response contains an invalid place ID.")
            if isinstance(raw_id, int):
                place_id = raw_id
            elif isinstance(raw_id, str) and raw_id.isdecimal():
                place_id = int(raw_id)
            else:
                raise AIResponseError("AI response contains an invalid place ID.")

            if place_id not in allowed_ids or place_id in seen_ids:
                raise AIResponseError("AI response contains an unknown or duplicate place ID.")

            seen_ids.add(place_id)
            parsed_place_ids.append(place_id)

        parsed_days[day] = parsed_place_ids

    expected_days = set(range(1, total_days + 1))
    if set(parsed_days) != expected_days:
        raise AIResponseError("AI response must include each trip day exactly once.")
    if seen_ids != allowed_ids:
        raise AIResponseError("AI response must include every saved sightseeing place.")

    return parsed_days


def _prompt_for_trip(trip, places):
    trip_data = {
        "origin": trip.source,
        "destination": trip.destination,
        "start_date": trip.start_date.isoformat(),
        "end_date": trip.end_date.isoformat(),
        "travellers": trip.number_of_people,
        "budget": str(trip.estimated_budget),
        "travel_mode": trip.travel_mode,
    }
    place_data = [
        {
            "id": place.id,
            "name": place.name,
            "category": place.category or "",
            "visit_minutes": max(int(place.time_required or 60), 30),
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
            "description": (place.description or "")[:160],
            "estimated_cost": str(place.estimated_cost),
        }
        for place in places
    ]
    return json.dumps(
        {"trip": trip_data, "places": place_data},
        ensure_ascii=False,
    )


AI_READ_TIMEOUT = 60
# Below this many seconds the AI rarely answers in time, so skip it.
AI_MIN_SECONDS = 10


def _cloudflare_schedule(prompt, read_timeout=AI_READ_TIMEOUT):
    return _cloudflare_json_completion(
        SYSTEM_PROMPT, prompt, 2048, read_timeout=read_timeout
    )


def _cloudflare_json_completion(
    system_prompt, prompt, max_tokens, read_timeout=AI_READ_TIMEOUT
):
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
    )
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.CLOUDFLARE_AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "reasoning_effort": "low",
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "store": False,
            "stream": False,
        },
        timeout=(5, read_timeout),
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise AIResponseError("Cloudflare AI returned no completion choices.")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise AIResponseError("Cloudflare AI returned an empty response.")
    return json.loads(content)


def build_ai_schedule(trip, places, time_budget=None):
    """Return a validated day assignment, provider name, and fallback warning.

    `time_budget` is the seconds left in the web request; the AI call never
    runs longer than that, so a slow provider cannot get the worker killed.
    """
    if not places:
        return None, None, None

    if time_budget is not None and time_budget < AI_MIN_SECONDS:
        return (
            None,
            None,
            "Finding places took too long, so AI was skipped and the "
            "standard planner was used. Regenerate to try AI again.",
        )

    if not all((
        settings.CLOUDFLARE_ACCOUNT_ID,
        settings.CLOUDFLARE_API_TOKEN,
        settings.CLOUDFLARE_AI_MODEL,
    )):
        return (
            None,
            None,
            "AI was requested, but Cloudflare Workers AI is not configured; "
            "the standard planner was used.",
        )

    total_days = max((trip.end_date - trip.start_date).days + 1, 1)
    place_ids = [place.id for place in places]
    prompt = _prompt_for_trip(trip, places)
    provider_name = f"cloudflare-workers-ai:{settings.CLOUDFLARE_AI_MODEL}"
    try:
        read_timeout = AI_READ_TIMEOUT
        if time_budget is not None:
            read_timeout = min(AI_READ_TIMEOUT, time_budget - 5)
        response = _cloudflare_schedule(prompt, read_timeout=read_timeout)
        schedule = validate_ai_schedule(response, place_ids, total_days)
        return schedule, provider_name, None
    except (requests.RequestException, ValueError) as error:
        logger.warning(
            "Cloudflare Workers AI itinerary request failed (%s).",
            type(error).__name__,
        )
        return (
            None,
            None,
            "Cloudflare Workers AI was unavailable or returned an invalid itinerary; "
            "the standard planner was used.",
        )
