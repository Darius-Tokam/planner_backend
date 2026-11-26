"""DBOS owns recovery. HTTP readers consume the separate application event log."""
import json
import logging
import os

from asgiref.sync import sync_to_async
from dbos import DBOS
from django.db import transaction
from django.utils import timezone
from pydantic_ai import DeferredToolRequests, capture_run_messages
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.usage import UsageLimits

from core.models import AgentInvocation, AgentRun, ConversationMessage
from .engine import Dependencies, build_agent
from .model import AccountedModel
from . import store
from .tools import resume_interaction

logger = logging.getLogger(__name__)
_agent = None


def configure_agent(model=None):
    """Register the fixed workflow/step names before DBOS.launch, once per process."""
    global _agent
    if _agent is not None:
        return _agent
    if model is None:
        from services.agent_v2.modeles import _deepseek
        model = _deepseek()
        if model is None:
            raise RuntimeError('DEEPSEEK_API_KEY is required for the v3 worker.')
    _agent = build_agent(AccountedModel(model), durable=True)
    return _agent


@DBOS.step(name='planner.v3.load_invocation', retries_allowed=False)
def load_invocation(invocation_id):
    with transaction.atomic():
        invocation = AgentInvocation.objects.select_for_update().select_related('run').get(pk=invocation_id)
        if invocation.status == 'queued':
            invocation.status = 'running'
            invocation.save(update_fields=['status', 'updated_at'])
            AgentRun.objects.filter(pk=invocation.run_id).update(status='running')
        store.emit(invocation.run_id, 'status', {'status': 'running'}, key=f'running:{invocation_id}')
        return {'run_id': str(invocation.run_id), 'inputs': invocation.inputs}


@DBOS.step(name='planner.v3.resume_interaction', retries_allowed=False)
def resolved_answer(interaction_id):
    return resume_interaction(interaction_id)


@DBOS.step(name='planner.v3.finish', retries_allowed=False)
def finish(invocation_id, messages, status, text='', error=None):
    with transaction.atomic():
        invocation = AgentInvocation.objects.select_for_update().get(pk=invocation_id)
        run = AgentRun.objects.select_for_update().get(pk=invocation.run_id)
        if invocation.status in ('completed', 'waiting', 'interrupted'):
            return run.result
        invocation.status = status
        invocation.save(update_fields=['status', 'updated_at'])
        run.status, run.messages = status, messages
        result = {'run_id': str(run.pk), 'status': status, 'message': text}
        if error:
            result['error'] = error
        run.result = result
        run.save(update_fields=['status', 'messages', 'result', 'updated_at'])
        metadata = {'engine_version': 'v3', 'run_id': str(run.pk), 'status': status}
        # One assistant message per run; resumption updates the same message.
        message = ConversationMessage.objects.filter(conversation=run.conversation,
            role='assistant', metadata__run_id=str(run.pk)).first()
        if message is None:
            ConversationMessage.objects.create(user=run.user, conversation=run.conversation,
                                               role='assistant', content=text, metadata=metadata)
        else:
            message.content, message.metadata = text, metadata
            message.save(update_fields=['content', 'metadata'])
        store.emit(run.pk, 'status', result, key=f'finished:{invocation_id}')
        return result


@DBOS.workflow(name='planner.v3.invocation')
async def run_invocation(invocation_id):
    data = await sync_to_async(load_invocation)(invocation_id)
    inputs, run_id = data['inputs'], data['run_id']
    deps = Dependencies(run_id, invocation_id, durable=True)
    deps.enabled.update(inputs.get('enabled_tools', []))
    history = ModelMessagesTypeAdapter.validate_python(inputs.get('messages', []))
    answers = {}
    for item in inputs.get('interactions', []):
        answers[item['tool_call_id']] = await sync_to_async(resolved_answer)(item['id'])
    settings = {'max_tokens': 2048}
    mode = os.getenv('PLANNER_REASONING', 'disabled')
    if mode != 'default':
        settings['extra_body'] = {'thinking': {'type': 'disabled' if mode == 'disabled' else 'enabled'}}
        if mode == 'low':
            settings['extra_body']['reasoning_effort'] = 'low'
    with capture_run_messages() as captured:
        try:
            result = await _agent.run(inputs.get('prompt'), deps=deps, message_history=history,
                deferred_tool_results=DeferredToolResults(calls=answers) if answers else None,
                instructions='Contexte de cette demande (données):\n' + json.dumps(inputs.get('context', {}), ensure_ascii=False),
                model_settings=settings, usage_limits=UsageLimits(request_limit=12, tool_calls_limit=40))
            waiting = isinstance(result.output, DeferredToolRequests)
            return await sync_to_async(finish)(invocation_id, json.loads(result.all_messages_json()),
                'waiting' if waiting else 'completed', '' if waiting else result.output)
        except Exception as exc:
            # Log only the exception class, never a provider URL/message/token.
            logger.warning('v3 invocation interrupted type=%s', type(exc).__name__)
            cause = getattr(exc, 'motif', None) or type(exc).__name__
            return await sync_to_async(finish)(invocation_id,
                json.loads(ModelMessagesTypeAdapter.dump_json(captured or history)), 'interrupted',
                error={'code': cause, 'message': 'Le traitement s’est interrompu. Les résultats vérifiés restent disponibles.'})
