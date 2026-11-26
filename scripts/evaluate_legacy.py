"""Matched-provider latency baseline, synthetic local accounts only.

The legacy control path is preserved. JEV and fallback providers are disabled;
this measures a reproducible local baseline, not production latency.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument('--env-file', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
url = urlparse(os.environ.get('DATABASE_URL', ''))
if url.hostname not in ('127.0.0.1', 'localhost') or url.path != '/planner_refonte':
    raise SystemExit('Isolated local planner_refonte required.')
from dotenv import dotenv_values
os.environ['DEEPSEEK_API_KEY'] = dotenv_values(args.env_file).get('DEEPSEEK_API_KEY', '')
for key in ('JEV_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'TYPESAFE_API_KEY'):
    os.environ[key] = ''
os.environ['JUGEMENT_REPLI_LLM'] = '0'
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from core.models import Conversation, Task, Goal
from services.agent_v2.agent import PlannerAgentV2
from services.agent_v2.budget_tour import statut_usage


def model():
    return OpenAIChatModel('deepseek-flash', settings={'extra_body': {'thinking': {'type': 'disabled'}}},
        provider=DeepSeekProvider(openai_client=AsyncOpenAI(api_key=settings.DEEPSEEK_API_KEY,
            base_url='https://api.deepseek.com', timeout=60, max_retries=0)))


ids = {'greet1', 'task1', 'read_null', 'task_two', 'task_delete', 'task_missing', 'event_missing', 'task_recurrence'}
cases = [case for case in json.loads((ROOT / 'scripts/runtime_corpus.json').read_text(encoding='utf-8')) if case['id'] in ids]
if len(cases) != 8:
    raise SystemExit(f'Expected eight matched cases, got {len(cases)}.')
rows = []
for case in cases:
    user = get_user_model().objects.create_user(f'baseline-{uuid4().hex[:16]}')
    user.profile.ai_consent_at = timezone.now()
    user.profile.save()
    Task.objects.create(user=user, title='Rapport')
    Goal.objects.create(user=user, title='ProveIt', goal_type='short_term')
    conversation = Conversation.objects.create(user=user, engine_version='v2')
    started, first, response = time.monotonic(), None, ''
    with patch('services.agent_v2.agent.modele_agir', side_effect=model):
        for event in PlannerAgentV2(user).process_message_stream(user, case['prompt'],
                conversation_id=conversation.pk, generate_quick_replies=False):
            if event.get('type') == 'delta' and event.get('text'):
                first = first if first is not None else time.monotonic() - started
            if event.get('type') == 'done':
                response = event.get('response', '')
    elapsed = time.monotonic() - started
    row = {'id': case['id'], 'elapsed_s': round(elapsed, 3),
           'first_text_s': round(first, 3) if first is not None else None,
           'measured_tokens': statut_usage(user)['jetons_mesures'], 'public_text': response,
           'task_count': Task.objects.filter(user=user).count()}
    rows.append(row)
    Path(args.output).write_text(json.dumps({'date': timezone.now().isoformat(), 'model': 'deepseek-flash',
        'reasoning': 'disabled', 'judge': 'disabled', 'concurrency': 1, 'results': rows,
        'p50_s': statistics.median(r['elapsed_s'] for r in rows)}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{row['id']} {row['elapsed_s']}s first={row['first_text_s']} tokens={row['measured_tokens']}", flush=True)
