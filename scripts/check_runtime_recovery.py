"""Real PostgreSQL/DBOS process crash proof. Dedicated local database only.

DEBUG=True DATABASE_URL=postgresql://.../planner_refonte python scripts/check_runtime_recovery.py
Run manage.py migrate on that database first. No production credentials accepted.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
url = os.environ.get('DATABASE_URL', '')
parsed = urlparse(url)
if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/planner_refonte':
    raise SystemExit('Dedicated local planner_refonte PostgreSQL database required.')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
import django
django.setup()

from dbos import DBOS, SetWorkflowID
from django.contrib.auth import get_user_model
from core.models import AgentRun, AgentOperation, Conversation, Task
from services.agent_runtime.store import commit_operation


@DBOS.step(name='planner_proof_write', retries_allowed=False)
def write_once(run_id):
    run = AgentRun.objects.get(pk=run_id)
    def effect():
        task = Task.objects.create(user_id=run.user_id, title='Crash proof')
        return {'success': True, 'data': {'id': task.pk}}
    result = commit_operation(run_id, 'stable-call', 'create_task', {'title': 'Crash proof'}, effect)
    if os.environ.get('PLANNER_PROOF_CRASH') == '1':
        os._exit(86)  # Deliberately after business commit, before DBOS step acknowledgement.
    return result


@DBOS.workflow(name='planner_crash_proof')
def workflow(run_id):
    return write_once(run_id)


def child(run_id):
    DBOS(config={'name': 'planner-crash-proof', 'system_database_url': url,
                 'application_version': 'proof-v1', 'executor_id': 'proof-worker',
                 'enable_otlp': False, 'log_level': 'ERROR'})
    DBOS.launch()
    with SetWorkflowID(f'proof-{run_id}'):
        result = workflow(run_id)
    assert result['success']
    DBOS.destroy()


if __name__ == '__main__':
    if len(sys.argv) == 2:
        child(sys.argv[1])
    else:
        user = get_user_model().objects.create_user(f'crash-proof-{uuid4().hex[:12]}')
        conversation = Conversation.objects.create(user=user, engine_version='v3')
        run = AgentRun.objects.create(user=user, conversation=conversation, payload_hash='proof')
        args = [sys.executable, __file__, str(run.pk)]
        first = subprocess.run(args, env={**os.environ, 'PLANNER_PROOF_CRASH': '1'}, timeout=90)
        assert first.returncode == 86, first.returncode
        assert Task.objects.filter(user=user).count() == 1
        second = subprocess.run(args, env={**os.environ, 'PLANNER_PROOF_CRASH': '0'}, timeout=90)
        assert second.returncode == 0, second.returncode
        assert Task.objects.filter(user=user).count() == 1
        assert AgentOperation.objects.filter(run=run).count() == 1
        print('PASS PostgreSQL + DBOS: crash after commit, one effect, one receipt, successful replay.')
