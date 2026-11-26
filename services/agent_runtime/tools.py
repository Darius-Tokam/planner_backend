import copy
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from core.models import AgentInteraction, AgentRun
from services.agent.tools import TOOL_MAP
from services.agent.tools.base import ToolResult
from services.agent_v2.registre import Registre
from services.agent_v2.reconciliation import reconcilier
from . import policy, store
from .questions import Question


def failure(cause, message, data=None):
    return {'success': False, 'issue': 'echec', 'cause': cause, 'message': message, 'data': data or {}}


def schema_for(name):
    if name == 'ask_question_custom':
        return Question.model_json_schema()
    schema = copy.deepcopy(TOOL_MAP[name].PARAMETERS)
    schema.get('properties', {}).pop('confirm', None)
    if 'required' in schema:
        schema['required'] = [key for key in schema['required'] if key != 'confirm']
    schema['additionalProperties'] = False
    return schema


def arguments_for(name, arguments):
    if not isinstance(arguments, dict) or any(key.startswith('_') for key in arguments):
        return None, failure('invalid_arguments', 'Paramètres privés ou invalides.')
    arguments = dict(arguments)
    arguments.pop('confirm', None)  # A model-supplied flag is never consent.
    model = Question if name == 'ask_question_custom' else TOOL_MAP[name].Parametres
    unknown = arguments.keys() - model.model_fields.keys()
    if unknown:
        return None, failure('invalid_arguments', 'Champs inconnus.', {'fields': sorted(unknown)})
    if 'confirm' in model.model_fields:
        arguments['confirm'] = False
    try:
        validated = model.model_validate(arguments).model_dump(exclude_unset=True, mode='json')
    except ValidationError as error:
        return None, failure('invalid_arguments', 'Corrige les champs invalides et rappelle l’outil.',
            {'fields': [{'path': list(e['loc']), 'type': e['type']} for e in error.errors()]})
    validated.pop('confirm', None)
    return validated, None


def waiting(interaction):
    return {'status': 'waiting', 'interaction_id': str(interaction.pk),
            'tool_call_id': interaction.tool_call_id, 'kind': interaction.kind,
            'question': interaction.question, 'expires_at': interaction.expires_at.isoformat()}


def defer(run, call_id, kind, question, prepared=None):
    interaction, _ = AgentInteraction.objects.get_or_create(run=run, tool_call_id=call_id,
        defaults={'kind': kind, 'question': question, 'prepared': prepared or {},
                  'expires_at': timezone.now() + timedelta(hours=24 if kind != 'confirmation' else 1)})
    result = waiting(interaction)
    store.emit(run.pk, 'interaction', result, key=f'interaction:{interaction.pk}')
    return result


def execute_call(run_id, call_id, name, arguments):
    result = _execute_call_db(run_id, call_id, name, arguments)
    if name == 'send_notification' and result.get('cause') == 'delivery_pending':
        from .delivery import deliver
        return deliver(result['operation_id'])
    return result


def _execute_call_db(run_id, call_id, name, arguments):
    if name not in TOOL_MAP or name in policy.EXCLUDED:
        return failure('unknown_tool', 'Cet outil n’est pas disponible. Consulte le catalogue.')
    args, error = arguments_for(name, arguments)
    if error:
        return error
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().select_related('user').get(pk=run_id)
        get_user_model().objects.select_for_update().get(pk=run.user_id)
        existing = run.operations.filter(tool_call_id=call_id).first()
        if existing:
            if existing.arguments_hash != store.digest({'tool': name, 'arguments': args}):
                return failure('call_identity_conflict', 'Cet identifiant d’appel a déjà été utilisé.')
            return existing.result
        if name == 'ask_question_custom':
            return defer(run, call_id, args['kind'], args)
        try:
            targets = policy.snapshot(run.user, name, args)
        except ObjectDoesNotExist:
            return failure('target_not_found', 'La cible n’existe pas dans ce compte. Relis les éléments disponibles.')
        signature = store.digest({'tool': name, 'arguments': args})
        if policy.confirmation_required(name, args):
            refused = run.interactions.filter(kind='confirmation', status='answered',
                prepared__signature=signature, answer__approved=False).exists()
            if refused:
                return failure('user_declined', 'L’utilisateur a refusé cette action. Continue les autres demandes indépendantes.')
            return defer(run, call_id, 'confirmation', {
                'question': 'Confirmer cette action ?', 'tool': name, 'arguments': args,
                'targets': targets['targets']}, {'tool': name, 'arguments': args,
                'signature': signature, 'snapshot': targets})
        return perform(run, call_id, name, args)


def perform(run, call_id, name, args, *, approved=False, targets=None):
    def effect():
        if name == 'send_notification':
            from core.models import AgentDelivery, AgentOperation
            from services.notification_delivery import has_device
            if not has_device(run.user):
                return {**failure('aucun_appareil', 'Aucun appareil abonné. Rien n’a été envoyé.'),
                        'notification_activation_required': True}
            operation = AgentOperation.objects.get(run=run, tool_call_id=call_id)
            AgentDelivery.objects.create(operation=operation, payload=args)
            return {'success': True, 'issue': 'effet_a_verifier', 'cause': 'delivery_pending',
                    'data': {}, 'message': 'Notification préparée; envoi non encore confirmé.'}
        values = dict(args)
        if approved:
            values['confirm'] = True
            values['_target_ids'] = targets or {}
        values['_operation_id'] = call_id
        result = TOOL_MAP[name].execute(run.user, **values)
        if not isinstance(result, ToolResult):
            raise TypeError('Invalid tool result contract')
        payload = result.to_dict()
        if name in policy.MUTATIONS:
            registry = Registre()
            action = registry.ajouter(name, args, result, cle_operation=call_id)
            reconcilier(run.user, registry)
            payload['receipt'] = registry.par_id(action.id).recu
            if result.success and payload['receipt']['statut'] not in ('confirme', 'sans_mutation'):
                payload['issue'] = 'effet_a_verifier'
                payload['cause'] = 'receipt_unverified'
                payload['message'] += '\nLa relecture ne confirme pas cet effet. Relis la cible avant toute autre écriture.'
        if name in ('manage_reminder', 'manage_recurrence', 'create_task', 'update_task'):
            from services.notification_delivery import has_device
            payload['notification_activation_required'] = not has_device(run.user)
        return payload
    result = store.commit_operation(run.pk, call_id, name, args, effect)
    return result


def resume_interaction(interaction_id):
    found = AgentInteraction.objects.get(pk=interaction_id)
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().select_related('user').get(pk=found.run_id)
        get_user_model().objects.select_for_update().get(pk=run.user_id)
        interaction = AgentInteraction.objects.select_for_update().get(pk=found.pk)
        if interaction.status != 'answered':
            return failure('answer_missing', 'Cette interaction attend une réponse valide.')
        if interaction.kind != 'confirmation':
            return {'success': True, 'data': {'answer': interaction.answer},
                    'message': 'Réponse de l’utilisateur à cette question précise.'}
        prepared = interaction.prepared
        # A committed receipt takes precedence over later target changes.
        existing = run.operations.filter(tool_call_id=interaction.tool_call_id).first()
        if existing:
            return existing.result
        if not interaction.answer.get('approved'):
            return failure('user_declined', 'L’utilisateur a refusé cette action. Continue les autres demandes indépendantes.')
        if interaction.expires_at <= timezone.now():
            return failure('confirmation_expired', 'Confirmation expirée. Prépare une nouvelle demande.')
        name, args = prepared['tool'], prepared['arguments']
        try:
            current = policy.snapshot(run.user, name, args)
        except ObjectDoesNotExist:
            return failure('target_changed', 'La cible a changé ou disparu. Relis-la avant de proposer une action.')
        if current != prepared['snapshot']:
            return failure('target_changed', 'La cible a changé depuis la confirmation. Aucune action exécutée.')
        targets = {}
        for target in current['targets']:
            targets.setdefault(target['model'], []).append(target['id'])
        return perform(run, interaction.tool_call_id, name, args, approved=True, targets=targets)
