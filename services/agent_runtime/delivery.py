"""Outbox with explicit unknown delivery, because APNs has no exactly-once API."""
from django.db import transaction

from core.models import AgentDelivery, AgentOperation
from .store import emit


def deliver(operation_id):
    with transaction.atomic():
        delivery = AgentDelivery.objects.select_for_update().select_related('operation__run__user').get(pk=operation_id)
        if delivery.status in ('completed', 'failed', 'unknown'):
            return delivery.result
        operation = delivery.operation
        if delivery.status == 'sending':
            result = {**operation.result, 'success': False, 'cause': 'delivery_unknown',
                      'issue': 'effet_a_verifier', 'message': 'Envoi interrompu : réception inconnue. Aucun renvoi automatique.'}
            delivery.status, delivery.result = 'unknown', result
            delivery.save()
            operation.result = result
            operation.save(update_fields=['result'])
            emit(operation.run_id, 'tool_result', result, key=f'delivery:{operation_id}')
            return result
        delivery.status = 'sending'
        delivery.save(update_fields=['status', 'updated_at'])
        payload = dict(delivery.payload)
        user = operation.run.user
    try:
        from services.notification_delivery import send_visible
        count = send_visible(user, payload.get('title') or 'Planner AI', payload['body'], url='/')
        result = {**operation.result, 'success': count > 0, 'issue': 'terminee' if count else 'echec',
                  'cause': '' if count else 'envoi_echoue', 'data': {'accepted_devices': count},
                  'message': f'Notification acceptée pour {count} appareil(s). L’affichage dépend du système.',
                  'receipt': {'source': 'provider', 'statut': 'accepted' if count else 'failed'}}
        status = 'completed' if count else 'failed'
    except Exception:
        result = {**operation.result, 'success': False, 'issue': 'effet_a_verifier',
                  'cause': 'delivery_unknown', 'message': 'Réception inconnue. Aucun renvoi automatique.'}
        status = 'unknown'
    with transaction.atomic():
        AgentDelivery.objects.filter(pk=operation_id).update(status=status, result=result)
        AgentOperation.objects.filter(pk=operation_id).update(result=result)
        emit(operation.run_id, 'tool_result', result, key=f'delivery:{operation_id}')
    return result
