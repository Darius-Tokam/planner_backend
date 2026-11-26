"""
Commute engine — departure-time math for events that happen somewhere else.

Implements the spec formula, in minutes-since-midnight (the scheduler's native
unit), as pure functions so they are trivially testable and reusable by the
scheduler, the reminders cron, and (later) a live maps-API refresher:

    heure_de_depart       = début_événement - durée_trajet - marge_de_sécurité
    début_indisponibilité = heure_de_depart - temps_de_préparation

No flexible activity may extend past début_indisponibilité; the window
[début_indisponibilité, début_événement] belongs to prep + travel. Symmetrically
the user is unavailable for `durée_trajet` after the event ends (return trip).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommuteWindow:
    """Resolved commute times around one event, in minutes since midnight.

    Values can be negative when the event is so early the preparation starts
    before midnight; callers clip to the day they are building.
    """
    event_start: int
    departure: int          # heure limite de départ
    unavailability_start: int  # heure à laquelle il faut commencer à se préparer
    travel_minutes: int
    safety_margin_minutes: int
    prep_minutes: int


def latest_departure(event_start_min: int, travel_minutes: int, safety_margin_minutes: int = 0) -> int:
    """Latest time to leave and still arrive `safety_margin_minutes` early."""
    return event_start_min - travel_minutes - safety_margin_minutes


def unavailability_start(departure_min: int, prep_minutes: int = 0) -> int:
    """Time at which the user must start getting ready (no flexible activity after)."""
    return departure_min - prep_minutes


def commute_window(
    event_start_min: int,
    travel_minutes: int,
    safety_margin_minutes: int = 0,
    prep_minutes: int = 0,
) -> CommuteWindow:
    """Full commute window for one event start."""
    departure = latest_departure(event_start_min, travel_minutes, safety_margin_minutes)
    return CommuteWindow(
        event_start=event_start_min,
        departure=departure,
        unavailability_start=unavailability_start(departure, prep_minutes),
        travel_minutes=travel_minutes,
        safety_margin_minutes=safety_margin_minutes,
        prep_minutes=prep_minutes,
    )


def commute_parts(item, profile, *, recurring: bool) -> dict:
    """The same policy feeds interval arithmetic and the public explanation."""
    place = getattr(item, 'place', None)
    travel = getattr(place, 'travel_minutes', 0) or 0
    if travel > 0:
        return dict(travel=travel, prep=getattr(profile, 'prep_time_minutes', 0) or 0,
                    margin=getattr(profile, 'safety_margin_minutes', 0) or 0, policy='place')
    if recurring:
        return dict(travel=getattr(profile, 'transport_time_minutes', 0) or 0, policy='profile_transport')
    return {}



def block_commute_minutes(block, profile) -> tuple:
    parts = commute_parts(block, profile, recurring=True)
    travel = parts.get('travel', 0)
    return travel + parts.get('prep', 0) + parts.get('margin', 0), travel


def task_commute_minutes(task, profile) -> tuple:
    parts = commute_parts(task, profile, recurring=False)
    travel = parts.get('travel', 0)
    return travel + parts.get('prep', 0) + parts.get('margin', 0), travel
