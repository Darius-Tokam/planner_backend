"""Policies use typed arguments and owned rows, never a lexical intent classifier."""
from django.forms.models import model_to_dict

from core.models import (Task, RecurringBlock, Goal, Reminder, RecurrenceSeries,
                         UploadedDocument, ScheduledBlock, UserProfile,
                         RecurringBlockException, RecurringBlockOverride)
from .catalog import TOOL_MAP
from services.agent_v2.registre import OUTILS_DE_MUTATION
from .store import digest

MUTATIONS = OUTILS_DE_MUTATION
EXCLUDED = {'present_form', 'present_choices', 'poser_question'}
TARGETS = {'task_id': Task, 'block_id': RecurringBlock, 'goal_id': Goal,
           'reminder_id': Reminder, 'series_id': RecurrenceSeries, 'document_id': UploadedDocument}


def confirmation_required(name, args):
    return (getattr(TOOL_MAP[name], 'REQUIRES_CONFIRMATION', False)
            or name in {'delete_block', 'cancel_scheduled_block', 'skip_block_occurrence'}
            or name == 'manage_reminder' and args.get('action') == 'cancel'
            or name == 'manage_recurrence' and args.get('action') in {'cancel', 'update', 'stop'}
            or name in {'organize_day', 'optimize_week'} and args.get('apply') is True)


def snapshot(user, name, args):
    """Locks concrete targets; hashes full fields, not an unreliable updated_at."""
    rows = []
    for key, model in TARGETS.items():
        if args.get(key) is not None:
            manager = getattr(model, 'all_objects', model.objects)
            obj = manager.select_for_update().get(user=user, pk=args[key])
            rows.append({'model': model.__name__, 'id': obj.pk, 'fields': model_to_dict(obj)})
    # Capture the complete scheduling inputs for operations affecting a set.
    if name in {'clear_all_blocks', 'cancel_scheduled_block', 'organize_day', 'optimize_week', 'skip_block_occurrence'}:
        for model in (Task, RecurringBlock, ScheduledBlock, RecurringBlockException,
                      RecurringBlockOverride, UserProfile):
            for obj in model.objects.select_for_update().filter(user=user).order_by('pk'):
                rows.append({'model': model.__name__, 'id': obj.pk, 'fields': model_to_dict(obj)})
    if name == 'delete_task' and args.get('task_id'):
        for obj in ScheduledBlock.objects.select_for_update().filter(user=user, task_id=args['task_id']).order_by('pk'):
            rows.append({'model': 'ScheduledBlock', 'id': obj.pk, 'fields': model_to_dict(obj)})
    if name == 'manage_recurrence' and args.get('series_id'):
        for model in (Task, Reminder):
            for obj in model.objects.select_for_update().filter(user=user, recurrence_id=args['series_id']).order_by('pk'):
                rows.append({'model': model.__name__, 'id': obj.pk, 'fields': model_to_dict(obj)})
    return {'fingerprint': digest(rows), 'targets': [
        {'model': r['model'], 'id': r['id'], 'title': r['fields'].get('title', '')} for r in rows]}
