"""Détection de chevauchement horaire — source de vérité unique.

Évite les "tâches sur tâches". Gère correctement les blocs qui traversent
minuit (night shift) en représentant chaque bloc hebdomadaire par un ou
plusieurs intervalles en "minutes de la semaine" (0..10080), où un bloc de
nuit déborde sur le jour suivant (avec rebouclage Dimanche -> Lundi).

Pur (pas d'I/O hormis la requête ORM dans find_recurring_conflicts), donc
facilement testable.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Iterable

MINUTES_PER_DAY = 1440
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY


def time_to_min(t: time) -> int:
    """Minutes depuis minuit (0..1439)."""
    return t.hour * 60 + t.minute


def parse_time(value) -> time:
    """Accepte un datetime.time ou une chaîne 'HH:MM' / 'HH:MM:SS'."""
    if isinstance(value, time):
        return value
    s = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Heure invalide: {value!r} (attendu 'HH:MM')")


def is_overnight(start: time, end: time, night_flag: bool = False) -> bool:
    """Un bloc traverse minuit si end <= start (ou si explicitement marqué)."""
    return bool(night_flag) or time_to_min(end) <= time_to_min(start)


def recurring_intervals(day_of_week: int, start: time, end: time,
                        night_flag: bool = False) -> list[tuple[int, int]]:
    """Intervalles [début, fin) en minutes de la semaine pour un bloc récurrent.

    - Bloc normal -> un seul intervalle dans le jour.
    - Bloc de nuit -> deux morceaux : [start, fin de journée) le jour J et
      [début de journée, end) le jour J+1 (rebouclage Dimanche->Lundi).
    """
    s = time_to_min(start)
    e = time_to_min(end)
    base = day_of_week * MINUTES_PER_DAY

    if not is_overnight(start, end, night_flag):
        if e == s:
            return []  # bloc de durée nulle -> aucun chevauchement
        return [(base + s, base + e)]

    # Overnight : déborde sur le jour suivant (rebouclage en fin de semaine).
    next_day = (day_of_week + 1) % 7
    next_base = next_day * MINUTES_PER_DAY
    first = (base + s, base + MINUTES_PER_DAY)
    second = (next_base, next_base + e)
    return [iv for iv in (first, second) if iv[1] > iv[0]]


def intervals_conflict(a: Iterable[tuple[int, int]],
                       b: Iterable[tuple[int, int]]) -> bool:
    """True si un intervalle de a chevauche un intervalle de b."""
    a = list(a)
    b = list(b)
    for a0, a1 in a:
        for b0, b1 in b:
            if a0 < b1 and b0 < a1:
                return True
    return False


def find_recurring_conflicts(
    user,
    day_of_week: int,
    start,
    end,
    night_flag: bool = False,
    exclude_id=None,
    *,
    new_is_flexible: bool = False,
    start_date=None, end_date=None,
):
    """Retourne les sources en conflit (titre, heures, planning_conflicts).

    `start`/`end` peuvent être des time ou des chaînes 'HH:MM'. Gère l'overnight
    et le débordement sur le jour adjacent (donc on inspecte J-1, J, J+1).
    """
    if new_is_flexible:
        return []

    from core.models import RecurringBlock, RecurringBlockException, RecurringBlockOverride, ScheduledBlock

    start_t = parse_time(start)
    end_t = parse_time(end)
    night = is_overnight(start_t, end_t, night_flag)
    new_iv = recurring_intervals(day_of_week, start_t, end_t, night)
    if not new_iv:
        return []

    # Un bloc de nuit déborde sur J+1 ; un bloc de nuit la veille déborde sur J.
    candidate_days = {
        day_of_week,
        (day_of_week + 1) % 7,
        (day_of_week - 1) % 7,
    }
    from django.db.models import Q
    from django.utils import timezone as _tz
    qs = RecurringBlock.objects.filter(
        # un bloc dont la fenetre est terminee ne bloque plus les nouveaux
        Q(end_date__isnull=True) | Q(end_date__gte=_tz.localdate()-timedelta(days=1)),
        user=user,
        active=True,
        day_of_week__in=candidate_days,
        flexibility='fixed',
    )
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)

    from services.scheduling.constraints import components, on_day, source_for
    from services.commute import commute_parts
    from types import SimpleNamespace
    profile = getattr(user, 'profile', None)
    proposal = RecurringBlock.objects.filter(user=user, pk=exclude_id).select_related('place').first() if exclude_id else None
    proposal = proposal or SimpleNamespace(place=None)
    base = day_of_week * MINUTES_PER_DAY
    ns, ne = base + time_to_min(start_t), base + time_to_min(end_t)
    if night:
        ne += MINUTES_PER_DAY
    proposed_source = {'type': 'requested_recurring', 'id': exclude_id,
        'title': 'Créneau demandé', 'day_of_week': day_of_week,
        'start_time': start_t.strftime('%H:%M'), 'end_time': end_t.strftime('%H:%M')}
    requested = components(ns, ne, proposed_source, **commute_parts(proposal, profile, recurring=True))
    conflicts = []
    # Exceptions and overrides are finite; inspect the first actual ordinary
    # occurrence, then check dated overrides separately below.
    exceptional = {}
    for model in (RecurringBlockException, RecurringBlockOverride):
        for block_id, exceptional_date in model.objects.filter(user=user).values_list('recurring_block_id', 'date'):
            exceptional.setdefault(block_id, set()).add(exceptional_date)
    for b in qs.select_related('place'):
        bs = b.day_of_week * MINUTES_PER_DAY + time_to_min(b.start_time)
        be = b.day_of_week * MINUTES_PER_DAY + time_to_min(b.end_time)
        if is_overnight(b.start_time, b.end_time, b.is_night_shift):
            be += MINUTES_PER_DAY
        source = {'type': 'recurring', 'id': b.pk, 'title': b.title,
                  'day_of_week': b.day_of_week, 'start_time': b.start_time.strftime('%H:%M'),
                  'end_time': b.end_time.strftime('%H:%M'), 'block_type': b.block_type}
        details = []
        for shift in (-MINUTES_PER_WEEK, 0, MINUTES_PER_WEEK):
            day_delta = (b.day_of_week * MINUTES_PER_DAY + shift - base) // MINUTES_PER_DAY
            lower = max(start_date or _tz.localdate(), _tz.localdate(),
                        b.start_date - timedelta(days=day_delta) if b.start_date else _tz.localdate())
            uppers = [d for d in (end_date, b.end_date - timedelta(days=day_delta) if b.end_date else None) if d is not None]
            upper = min(uppers) if uppers else None
            witness = lower + timedelta(days=(day_of_week - lower.weekday()) % 7)
            while (witness in exceptional.get(exclude_id, set())
                   or witness + timedelta(days=day_delta) in exceptional.get(b.pk, set())):
                witness += timedelta(days=7)
            if upper is not None and witness > upper:
                continue
            existing = components(bs + shift, be + shift, source, **commute_parts(b, profile, recurring=True))
            for left in requested:
                for right in existing:
                    if not right.overlaps(left.start, left.end):
                        continue
                    overlap_start, overlap_end = max(left.start, right.start), min(left.end, right.end)
                    chosen = right if right.kind != 'activity' or left.kind == 'activity' else left
                    day_offset = overlap_start // MINUTES_PER_DAY
                    for piece in on_day([chosen], -day_offset * MINUTES_PER_DAY):
                        detail = piece.as_dict((overlap_start - day_offset * MINUTES_PER_DAY,
                                                overlap_end - day_offset * MINUTES_PER_DAY))
                        detail.update(day_of_week=day_offset % 7, scope='weekly_templates',
                                      first_conflict_date=(witness + timedelta(days=day_offset-day_of_week)).isoformat(),
                                      with_source=source, requested_kind=left.kind, existing_kind=right.kind)
                        details.append(detail)
        if details:
            b.planning_conflicts = details
            conflicts.append(b)
    # Dated reservations cannot disappear behind a recurring-only validator.
    lower = max(start_date or _tz.localdate(), _tz.localdate())
    scheduled = ScheduledBlock.objects.filter(user=user, date__gte=lower-timedelta(days=1)).exclude(
        Q(actually_completed=True) | Q(task__completed=True))
    overrides = RecurringBlockOverride.objects.filter(user=user, recurring_block__user=user,
        recurring_block__active=True, date__gte=lower-timedelta(days=1)).exclude(recurring_block_id=exclude_id)
    if end_date:
        scheduled = scheduled.filter(date__lte=end_date+timedelta(days=1))
        overrides = overrides.filter(date__lte=end_date+timedelta(days=1))
    dated = [(b.date, b, b.task, True) for b in scheduled.select_related('task__place')]
    skipped = set(RecurringBlockException.objects.filter(user=user).values_list('recurring_block_id','date'))
    for override in overrides.select_related('recurring_block__place'):
        b = override.recurring_block
        if ((b.pk, override.date) in skipped or b.day_of_week != override.date.weekday()
            or b.start_date and override.date < b.start_date or b.end_date and override.date > b.end_date):
            continue
        b.start_time, b.end_time = override.start_time, override.end_time
        dated.append((override.date, b, b, False))
    for occupied_date, b, owner, is_task in dated:
        for delta in (-1, 0, 1):
            candidate_date = occupied_date + timedelta(days=delta)
            if (candidate_date.weekday() != day_of_week or candidate_date < lower
                or end_date and candidate_date > end_date
                or candidate_date in exceptional.get(exclude_id, set())):
                continue
            offset = -delta * MINUTES_PER_DAY
            s, e = time_to_min(b.start_time), time_to_min(b.end_time)
            if e <= s:
                e += MINUTES_PER_DAY
            source = source_for(b, occupied_date, scheduled=is_task)
            existing = components(s+offset, e+offset, source,
                **commute_parts(owner, profile, recurring=not is_task))
            details = []
            for left in requested:
                left = type(left)(left.start-base, left.end-base, left.kind, left.source, left.policy, left.direction)
                for right in existing:
                    if not right.overlaps(left.start, left.end):
                        continue
                    overlap_start, overlap_end = max(left.start,right.start), min(left.end,right.end)
                    chosen = right if right.kind != 'activity' or left.kind == 'activity' else left
                    for day_delta in range(overlap_start // MINUTES_PER_DAY, (overlap_end-1) // MINUTES_PER_DAY+1):
                        for piece in on_day([chosen], -day_delta*MINUTES_PER_DAY):
                            detail = piece.as_dict((max(0,overlap_start-day_delta*MINUTES_PER_DAY),
                                                    min(MINUTES_PER_DAY,overlap_end-day_delta*MINUTES_PER_DAY)))
                            detail.update(date=(candidate_date+timedelta(days=day_delta)).isoformat(),
                                scope='dated_reservation', with_source=source,
                                requested_kind=left.kind, existing_kind=right.kind)
                            details.append(detail)
            if details:
                conflicts.append(SimpleNamespace(title=source['title'], start_time=b.start_time,
                    end_time=b.end_time, planning_conflicts=details))
    return conflicts
