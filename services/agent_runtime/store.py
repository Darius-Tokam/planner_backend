import hashlib
import json
import logging
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from core.models import AgentEvent, AgentInteraction, AgentOperation, AgentRun

logger = logging.getLogger(__name__)


class Conflict(ValueError):
    pass


class InvalidAnswer(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def emit(run_id, kind, data, *, key=None):
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().get(pk=run_id)
        key = key or str(uuid4())
        existing = AgentEvent.objects.filter(run=run, key=key).first()
        if existing:
            return existing
        run.next_seq += 1
        run.save(update_fields=['next_seq', 'updated_at'])
        return AgentEvent.objects.create(run=run, seq=run.next_seq, kind=kind, data=data, key=key)


def events(user, run_id, after=0):
    run = AgentRun.objects.get(user=user, pk=run_id)
    return run.events.filter(seq__gt=after)


def commit_operation(run_id, call_id, tool, arguments, execute):
    """No callback retry. The receipt commits atomically with Django effects."""
    signature = digest({'tool': tool, 'arguments': arguments})
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().get(pk=run_id)
        existing = AgentOperation.objects.filter(run=run, tool_call_id=call_id).first()
        if existing:
            if existing.arguments_hash != signature:
                raise Conflict('Identité d’appel réutilisée avec des arguments différents.')
            return existing.result
        operation = AgentOperation.objects.create(run=run, tool_call_id=call_id, tool=tool,
            arguments_hash=signature, arguments=arguments)
        try:
            with transaction.atomic():
                result = execute()
                # Reject non-serializable results inside the savepoint, before commit.
                json.dumps(result)
        except Exception as exc:
            logger.warning('runtime tool failed tool=%s error_type=%s', tool, type(exc).__name__)
            result = {'success': False, 'data': {}, 'issue': 'echec', 'cause': 'tool_exception',
                      'message': 'L’outil a échoué; ses écritures ont été annulées.'}
        result = {**result, 'operation_id': str(operation.pk), 'tool_call_id': call_id, 'tool': tool}
        operation.result = result
        operation.save(update_fields=['result'])
        emit(run.pk, 'tool_result', result, key=f'operation:{operation.pk}')
        return result


def validate_answer(interaction, value):
    if not isinstance(value, dict):
        raise InvalidAnswer('Réponse structurée requise.')
    if interaction.kind == 'confirmation':
        if set(value) != {'approved'} or type(value['approved']) is not bool:
            raise InvalidAnswer('Une décision explicite est requise.')
    elif interaction.kind == 'text':
        if set(value) != {'text'} or not isinstance(value['text'], str) or not 1 <= len(value['text'].strip()) <= 4000:
            raise InvalidAnswer('Saisis une réponse de 1 à 4 000 caractères.')
    elif interaction.kind == 'choice':
        options = {str(option['id']) for option in interaction.question.get('options', [])}
        if set(value) != {'choice'} or str(value['choice']) not in options:
            raise InvalidAnswer('Choisis une des options proposées.')
    else:
        from .questions import validate_form_answer
        validate_form_answer(interaction.question, value)
    return value


def answer(user, interaction_id, value):
    with transaction.atomic():
        # Always lock the run before its interaction: same order as the worker.
        found = AgentInteraction.objects.get(pk=interaction_id, run__user=user)
        AgentRun.objects.select_for_update().get(pk=found.run_id)
        interaction = AgentInteraction.objects.select_for_update().get(pk=found.pk)
        value = validate_answer(interaction, value)
        if interaction.status == 'answered' and interaction.answer == value:
            return interaction
        if interaction.status != 'pending':
            raise Conflict('Cette question a déjà reçu une réponse.')
        if interaction.expires_at <= timezone.now():
            raise Conflict('Cette question a expiré. Demande une nouvelle vérification.')
        interaction.answer, interaction.status = value, 'answered'
        interaction.answered_at = timezone.now()
        interaction.save(update_fields=['answer', 'status', 'answered_at'])
        emit(interaction.run_id, 'interaction_answered', {'id': str(interaction.pk)},
             key=f'answer:{interaction.pk}')
        return interaction
