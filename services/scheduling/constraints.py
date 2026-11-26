"""Explainable occupancy. Merging is a projection, never the source of facts.

Intervals are half open [start, end), in local wall-clock minutes on one date.
Each component retains the activity and policy that caused it to be reserved.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q

from services.scheduling.occurrences import recurring_occurrences
from services.commute import commute_parts
from services.scheduling.overlap import is_overnight, time_to_min


def hhmm(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


@dataclass(frozen=True)
class Constraint:
    start: int
    end: int
    kind: str
    source: dict
    policy: str = 'activity'
    direction: str = ''
    movable: bool = False

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.end and self.start < end

    def as_dict(self, window=None) -> dict:
        result = dict(kind=self.kind, start_time=hhmm(self.start), end_time=hhmm(self.end),
                      source=self.source, policy=self.policy, movable=self.movable)
        if self.direction:
            result['direction'] = self.direction
        if window is not None:
            start, end = max(self.start, window[0]), min(self.end, window[1])
            result.update(overlap_start=hhmm(start), overlap_end=hhmm(end),
                          overlap_minutes=max(0, end - start))
        return result


def components(start: int, end: int, source: dict, *, travel=0, prep=0, margin=0,
               policy='activity', kind='activity') -> list[Constraint]:
    """Unclipped components, also used to compare recurring weekly templates."""
    result = [Constraint(start, end, kind, source)]
    cursor = start
    # Preparation, outward travel, safety margin, activity, return travel.
    for component, duration in (('safety_margin', margin), ('transport', travel), ('preparation', prep)):
        if duration:
            result.append(Constraint(cursor - duration, cursor, component, source, policy, 'before'))
            cursor -= duration
    if travel:
        result.append(Constraint(end, end + travel, 'transport', source, policy, 'after'))
    return result


def source_for(item, day, *, scheduled=False) -> dict:
    return dict(type='scheduled_task' if scheduled else 'recurring', id=item.pk,
                title=item.task.title if scheduled else item.title, date=day.isoformat(),
                start_time=item.start_time.strftime('%H:%M'), end_time=item.end_time.strftime('%H:%M'),
                **({'task_id': item.task_id} if scheduled else {'block_type': item.block_type}))


def on_day(parts, offset=0) -> list[Constraint]:
    result = []
    for part in parts:
        start, end = max(0, part.start + offset), min(1440, part.end + offset)
        if start < end:
            result.append(Constraint(start, end, part.kind, part.source, part.policy,
                                     part.direction, part.movable))
    return result


def fixed_constraints(user, day, *, exclude_scheduled_id=None) -> list[Constraint]:
    """Fixed activities and their margins, including adjacent-day spillovers."""
    from core.models import ScheduledBlock
    profile = getattr(user, 'profile', None)
    result = []
    scheduled = ScheduledBlock.objects.filter(user=user, date__range=(day-timedelta(days=2), day+timedelta(days=2))).exclude(
        Q(actually_completed=True) | Q(task__completed=True))
    if exclude_scheduled_id is not None:
        ids = exclude_scheduled_id if isinstance(exclude_scheduled_id, (tuple, list, set)) else [exclude_scheduled_id]
        scheduled = scheduled.exclude(pk__in=ids)
    scheduled = list(scheduled.select_related('task', 'task__place'))
    # schedule_task_at stores overnight events as two contiguous locked rows.
    # Do not invent an outward/return trip at the midnight seam.
    heads = {(b.task_id, b.date) for b in scheduled if b.locked and b.end_time.strftime('%H:%M') == '23:59'}
    tails = {(b.task_id, b.date) for b in scheduled if b.locked and b.start_time.strftime('%H:%M') == '00:00'}
    # Tomorrow's preparation may begin today; yesterday's return may end today.
    for shift in (-1, 0, 1):
        occurrence_date = day + timedelta(days=shift)
        for block in recurring_occurrences(user, occurrence_date):
            if block.is_flexible and not block.occurrence_override:
                continue
            start, end = time_to_min(block.start_time), time_to_min(block.end_time)
            if is_overnight(block.start_time, block.end_time, block.is_night_shift):
                end += 1440
            parts = components(start, end, source_for(block, occurrence_date),
                kind='sleep' if block.block_type == 'sleep' else 'activity',
                **(commute_parts(block, profile, recurring=True) if not block.is_flexible else {}))
            result.extend(on_day(parts, shift * 1440))
        for block in (b for b in scheduled if b.date == occurrence_date):
            start, end = time_to_min(block.start_time), time_to_min(block.end_time)
            if end <= start:
                end += 1440
            is_tail = block.locked and start == 0 and (block.task_id, occurrence_date-timedelta(days=1)) in heads
            is_head = block.locked and end == 1439 and (block.task_id, occurrence_date+timedelta(days=1)) in tails
            parts = components(start, 1440 if is_head else end, source_for(block, occurrence_date, scheduled=True),
                **commute_parts(block.task, profile, recurring=False))
            parts = [p for p in parts if not (is_tail and p.direction == 'before' or is_head and p.direction == 'after')]
            result.extend(on_day(parts, shift * 1440))
    return sorted(result, key=lambda c: (c.start, c.end, c.source['type'], c.source['id']))


def flexible_constraints(user, day, *, fixed=None) -> list[Constraint]:
    """Placed flexible occupancy and protected sleep, with original identities."""
    from services.scheduling.placement import _blocs_du_jour, _reports_du_jour, _placer_jour
    result = []
    previous = day - timedelta(days=1)
    blocks = _blocs_du_jour(user, previous)
    reports = _reports_du_jour(blocks, getattr(user, 'profile', None), previous)
    for block in blocks:
        if block.pk in reports:
            report = reports[block.pk]
            source = source_for(block, previous)
            result.extend(on_day([Constraint(report['start_min'], report['end_min'], 'sleep', source,
                                             'recovery_sleep')]))
    for placement in _placer_jour(user, day, fixed=fixed, reporte=[(c.start,c.end) for c in result]):
        if placement['skipped'] or placement.get('occurrence_override'):
            continue
        start, end = placement['start_min'], placement['end_min']
        if start is None or end is None:
            continue
        source = dict(type='recurring', id=placement['block_id'], title=placement['title'],
                      date=day.isoformat(), start_time=placement['start_time'], end_time=placement['end_time'],
                      block_type=placement['block_type'])
        sleep = placement['block_type'] == 'sleep'
        windows = [(start, end)] if end > start else [(0, end), (start, 1440)]
        for a, b in windows:
            result.extend(on_day([Constraint(a, b, 'sleep' if sleep else 'activity', source,
                'placed_sleep' if sleep else 'flexible_placement', movable=not sleep)]))
    return result


def day_constraints(user, day, *, exclude_scheduled_id=None, hard_only=False) -> list[Constraint]:
    result = fixed_constraints(user, day, exclude_scheduled_id=exclude_scheduled_id)
    fixed = [(c.start,c.end) for c in result]
    result.extend(c for c in flexible_constraints(user, day, fixed=fixed) if not hard_only or not c.movable)
    return sorted(result, key=lambda c: (c.start, c.end))


def describe_data(constraint: dict) -> str:
    labels = {'activity': 'activité', 'sleep': 'sommeil protégé', 'transport': 'transport',
              'preparation': 'préparation', 'safety_margin': 'marge de sécurité'}
    link = {'before': 'avant', 'after': 'après'}.get(constraint.get('direction'), 'pour')
    source = constraint['source']
    return (f"{labels[constraint['kind']]} ({constraint['start_time']}-{constraint['end_time']}) "
            f"{link} « {source['title']} » ({source['start_time']}-{source['end_time']})")


def describe(constraint: Constraint) -> str:
    return describe_data(constraint.as_dict())
