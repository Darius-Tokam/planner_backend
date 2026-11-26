"""Execute the real DBOSAgent adapter with a controlled provider and PostgreSQL."""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
url = os.environ.get('DATABASE_URL', '')
if urlparse(url).hostname not in ('127.0.0.1', 'localhost') or urlparse(url).path != '/planner_refonte':
    raise SystemExit('Dedicated local planner_refonte PostgreSQL database required.')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
import django
django.setup()

from dbos import DBOS, SetWorkflowID
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.messages import ToolReturnPart
from core.models import AgentRun, AgentInvocation, Conversation, Task, AgentModelAttempt
from services.agent_runtime.workflow import configure_agent, run_invocation


async def provider(messages, info):
    results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    if not results:
        yield {0: DeltaToolCall(name='create_task', tool_call_id='proof-create', json_args='{"title":"DBOS full loop"}')}
    else:
        assert results[-1].content['receipt']['statut'] == 'confirme'
        yield 'Tâche '
        await asyncio.sleep(.05)
        yield 'créée.'


async def execute(invocation):
    with SetWorkflowID(f'planner-v3-{invocation.pk}'):
        return await run_invocation(str(invocation.pk))


if __name__ == '__main__':
    user = get_user_model().objects.create_user(f'loop-proof-{uuid4().hex[:12]}')
    user.profile.ai_consent_at = timezone.now()
    user.profile.save()
    conv = Conversation.objects.create(user=user, engine_version='v3')
    run = AgentRun.objects.create(user=user, conversation=conv, payload_hash='proof')
    invocation = AgentInvocation.objects.create(run=run, number=0, inputs={'prompt': 'Crée une tâche DBOS full loop.'})
    DBOS(config={'name': 'planner-loop-proof', 'system_database_url': url, 'enable_otlp': False,
                 'application_version': 'loop-proof-v1', 'executor_id': 'loop-proof', 'log_level': 'ERROR'})
    configure_agent(FunctionModel(stream_function=provider))
    DBOS.launch()
    try:
        first = async_to_sync(execute)(invocation)
        assert first['status'] == 'completed', first
        second = async_to_sync(execute)(invocation)
        assert first == second
        assert Task.objects.filter(user=user).count() == 1
        assert AgentModelAttempt.objects.filter(invocation=invocation).count() == 2
        assert run.events.filter(kind='text').exists()
        from services.agent_runtime.purge import drain
        prefix = f'planner-v3-{invocation.pk}'
        assert DBOS.list_workflows(workflow_id_prefix=prefix, load_input=False, load_output=False)
        user.delete()
        drain(limit=1000)
        assert not DBOS.list_workflows(workflow_id_prefix=prefix, load_input=False, load_output=False)
        print('PASS DBOSAgent + PydanticAI + PostgreSQL: tool, receipt, streamed text, replay, usage unchanged, root and SDK children purged after account deletion.')
    finally:
        DBOS.destroy()
