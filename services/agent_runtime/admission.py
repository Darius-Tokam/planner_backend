import json
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import AgentInvocation, AgentRun, Conversation, ConversationMessage, UploadedDocument
from .context import bounded_history
from . import store

ACTIVE = ('queued', 'running', 'waiting')


def default_engine(user):
    cohort = {part.strip() for part in str(getattr(settings, 'PLANNER_V3_USER_IDS', '')).split(',')}
    return 'v3' if getattr(settings, 'PLANNER_RUNTIME_DEFAULT', 'v2') == 'v3' or str(user.pk) in cohort else 'v2'


def admit(user, conversation_id, turn_id, message, fingerprint, files=(), document_type='other'):
    turn_id = UUID(str(turn_id))
    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().get(pk=conversation_id, user=user, archivee=False)
        if conversation.engine_version != 'v3':
            raise store.Conflict('Cette conversation utilise le moteur précédent.')
        existing = AgentRun.objects.filter(pk=turn_id).first()
        if existing:
            if existing.user_id != user.pk or existing.conversation_id != conversation.pk or existing.payload_hash != fingerprint:
                raise store.Conflict('Cet identifiant appartient à une autre demande.')
            return existing
        if conversation.agent_runs.filter(status__in=ACTIVE).exists():
            raise store.Conflict('Termine la demande en cours ou réponds à sa question avant d’en envoyer une autre.')
        latest = conversation.agent_runs.filter(status='completed').order_by('-created_at').first()
        history, omitted = bounded_history(latest.messages if latest else [])
        now = timezone.localtime()
        context = {'message_received_at': now.isoformat(), 'date': now.date().isoformat(),
                   'timezone': timezone.get_current_timezone_name(), 'omitted_turns': omitted,
                   'name': user.first_name or user.username}
        docs = [UploadedDocument.objects.create(user=user, file=file, file_name=file.name,
                document_type=document_type) for file in files]
        context['documents'] = [{'id': doc.pk, 'name': doc.file_name} for doc in docs]
        prompt = message or 'Consulte les documents joints.'
        run = AgentRun.objects.create(id=turn_id, user=user, conversation=conversation,
                                     payload_hash=fingerprint, prompt=prompt, context=context)
        inputs = {'prompt': prompt, 'messages': history, 'context': context}
        if docs:
            inputs['enabled_tools'] = ['process_uploaded_document', 'resolve_document_entry']
        AgentInvocation.objects.create(run=run, number=0, inputs=inputs)
        msg = ConversationMessage.objects.create(user=user, conversation=conversation, role='user', content=message,
                                                metadata={'engine_version': 'v3', 'run_id': str(run.pk)})
        msg.attachments.set(docs)
        ConversationMessage.objects.create(user=user, conversation=conversation, role='assistant', content='',
            metadata={'engine_version': 'v3', 'run_id': str(run.pk), 'status': 'queued'})
        if conversation.titre == 'Nouvelle conversation':
            conversation.titre = prompt[:80]
        conversation.save(update_fields=['titre', 'updated_at'])
        store.emit(run.pk, 'accepted', {'run_id': str(run.pk), 'user_message_id': msg.pk}, key='accepted')
        return run


def resume(user, interaction_id, value):
    with transaction.atomic():
        interaction = store.answer(user, interaction_id, value)
        run = AgentRun.objects.select_for_update().get(pk=interaction.run_id)
        if run.status != 'waiting':
            return run
        pending_ids = run.result.get('pending_calls')
        interactions = run.interactions.all()
        if pending_ids is not None:
            interactions = interactions.filter(tool_call_id__in=pending_ids)
        if interactions.filter(status='pending').exists():
            return run
        last = run.invocations.order_by('-number').first()
        inputs = {'prompt': None, 'messages': run.messages, 'context': run.context,
                  'enabled_tools': last.inputs.get('enabled_tools', []),
                  'interactions': [{'id': str(item.pk), 'tool_call_id': item.tool_call_id}
                                   for item in interactions.filter(status='answered')]}
        AgentInvocation.objects.create(run=run, number=last.number + 1, inputs=inputs)
        run.status = 'queued'
        run.save(update_fields=['status', 'updated_at'])
        store.emit(run.pk, 'status', {'status': 'queued'}, key=f'resume:{last.number + 1}')
        return run


def snapshot(run, after=0):
    events = list(run.events.filter(seq__gt=after)[:500])
    return {'run_id': str(run.pk), 'conversation_id': run.conversation_id, 'status': run.status,
            'cursor': events[-1].seq if events else after, 'has_more': bool(events and events[-1].seq < run.next_seq),
            'events': [{'seq': e.seq, 'type': e.kind, 'data': e.data} for e in events], 'result': run.result}


def expire_waiting():
    """Expire without executing the deferred mutation or replaying an old answer."""
    ids = AgentRun.objects.filter(status='waiting', interactions__status='pending',
        interactions__expires_at__lte=timezone.now()).values_list('pk', flat=True).distinct()
    for run_id in list(ids):
        with transaction.atomic():
            run = AgentRun.objects.select_for_update().get(pk=run_id)
            if run.status != 'waiting' or not run.interactions.filter(
                    status='pending', expires_at__lte=timezone.now()).exists():
                continue
            run.status = 'cancelled'
            run.result = {'status': 'cancelled', 'error': {'code': 'question_expired',
                'message': 'Cette question a expiré. Aucune action en attente n’a été exécutée. Tu peux faire une nouvelle demande.'}}
            run.save(update_fields=['status', 'result', 'updated_at'])
            run.interactions.filter(status='pending').update(status='expired')
            store.emit(run.pk, 'status', run.result, key='expired')
