"""Real provider, real tools, isolated local PostgreSQL. No production fixtures."""
import argparse
import asyncio
from datetime import timedelta, time as walltime
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument('--env-file', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--repeat', type=int, default=1)
parser.add_argument('--railway-relay', action='store_true')
parser.add_argument('--case', action='append', default=[])
options = parser.parse_args()
url = os.environ.get('DATABASE_URL', '')
if urlparse(url).hostname not in ('localhost', '127.0.0.1') or urlparse(url).path != '/planner_refonte':
    raise SystemExit('Dedicated local planner_refonte database required.')
from dotenv import dotenv_values
key = dotenv_values(options.env_file).get('DEEPSEEK_API_KEY')
if not key:
    raise SystemExit('Provider key missing.')
os.environ['DEEPSEEK_API_KEY'] = key
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
os.environ['PLANNER_REASONING'] = 'disabled'
import django
django.setup()
from asgiref.sync import sync_to_async
from dbos import DBOS, SetWorkflowID
from django.contrib.auth import get_user_model
from django.utils import timezone
from core.models import Conversation, RecurringBlock, ScheduledBlock, Task, AgentRun, AgentModelAttempt
from services.agent_runtime.admission import admit, resume
from services.agent_runtime.store import digest
from services.agent_runtime.workflow import configure_agent, run_invocation
from services.agent_runtime.policy import MUTATIONS

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
batch = uuid4().hex[:10]
rows = []
CASES = [
    ('explain_transport', 'Pourquoi je ne suis pas libre de 12h40 à 13h le {day} alors que mon cours commence à 13h ?', 'explain', 20),
    ('conflict_resume', 'Planifie Marché de Noël le {day} de 11h40 à 13h.', 'resume', 20),
    ('touching_allowed', 'Planifie Sortie le {day} de 11h40 à 13h.', 'create', 0),
    ('recurring_conflict', 'Crée Travail fixe chaque {weekday} de 11h40 à 13h à partir du {day}.', 'question', 20),
    ('correction', "Non, je n'ai pas d'examen de conception. C'est un cours. Corrige ton explication.", 'correction', 20),
    ('detect_transport', 'Vérifie les conflits du {day} et explique précisément leur cause.', 'detect', 20),
    ('alternate_conflict', 'Réserve Dentiste le {day} de 15h à 15h15.', 'question', 20),
    ('social', 'Merci, et toi comment tu vas ?', 'social', 20),
]


def setup_case(case, iteration):
    name, prompt, expected, transport = case
    day = timezone.localdate() + timedelta(days=1)
    user = get_user_model().objects.create_user(f'constraints-{batch}-{iteration}-{name}')
    user.profile.ai_consent_at = timezone.now()
    user.profile.transport_time_minutes = transport
    user.profile.save()
    course = RecurringBlock.objects.create(user=user, title="Conception d'applications", block_type='course',
        day_of_week=day.weekday(), start_time=walltime(13), end_time=walltime(15), flexibility='fixed')
    task = Task.objects.create(user=user, title='Révision examen')
    ScheduledBlock.objects.create(user=user, task=task, date=day, start_time=walltime(15,20), end_time=walltime(16,20))
    if expected == 'detect':
        task = Task.objects.create(user=user, title='Sortie existante')
        ScheduledBlock.objects.create(user=user, task=task, date=day, start_time=walltime(12,45), end_time=walltime(13))
    conv = Conversation.objects.create(user=user, engine_version='v3')
    weekdays = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche']
    prompt = prompt.format(day=day.isoformat(), weekday=weekdays[day.weekday()])
    run = admit(user, conv.pk, uuid4(), prompt, digest(prompt))
    invocation = run.invocations.first()
    if expected == 'correction':
        # Synthetic prior mistake, matching the reported failure, no private production data.
        from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart, ModelMessagesTypeAdapter
        invocation.inputs['messages'] = json.loads(ModelMessagesTypeAdapter.dump_json([
            ModelRequest(parts=[UserPromptPart(f'Lis ma journée du {day.isoformat()}.')]),
            ModelResponse(parts=[TextPart("Ton examen de Conception d'applications est de 13h à 15h.")])]))
    run.status = invocation.status = 'running'
    run.save(update_fields=['status'])
    invocation.save(update_fields=['status','inputs'])
    return user.pk, course.pk, run.pk, invocation.pk, prompt


def inspect_run(user_id, course_id, run_id):
    run = AgentRun.objects.get(pk=run_id)
    operations = list(run.operations.all())
    questions = list(run.interactions.filter(status='pending'))
    attempts = AgentModelAttempt.objects.filter(invocation__run=run)
    return dict(status=run.status, text=run.result.get('message',''),
        tools=[op.tool for op in operations],
        effects=[op.tool for op in operations if op.tool in MUTATIONS and op.result.get('success')],
        failures=[op.result.get('cause') for op in operations if not op.result.get('success')],
        receipts=[op.result.get('receipt',{}).get('statut') for op in operations if op.tool in MUTATIONS and op.result.get('success')],
        questions=[{'id':str(q.pk),'kind':q.kind,'question':q.question} for q in questions],
        course_present=RecurringBlock.objects.filter(pk=course_id,user_id=user_id,active=True).exists(),
        created_slots=list(ScheduledBlock.objects.filter(user_id=user_id).exclude(task__title__in=['Révision examen','Sortie existante']).values('start_time','end_time','task__title')),
        calls=attempts.count(), tokens=sum(a.usage.get('total',0) for a in attempts))


def resume_case(user_id, run_id, question):
    assert question['kind'] != 'confirmation', 'Unexpected destructive confirmation'
    user = get_user_model().objects.get(pk=user_id)
    run = resume(user, question['id'], {'text':'Même événement, de 11h40 à 12h40. Garde le cours et le temps de transport.'})
    invocation = run.invocations.order_by('-number').first()
    run.status = invocation.status = 'running'
    run.save(update_fields=['status'])
    invocation.save(update_fields=['status'])
    return invocation.pk


async def invoke(invocation_id):
    with SetWorkflowID(f'constraints-{invocation_id}'):
        await run_invocation(str(invocation_id))


async def main():
    for iteration in range(options.repeat):
        for case in CASES:
            if options.case and case[0] not in options.case:
                continue
            started = time.monotonic()
            user_id, course_id, run_id, invocation_id, prompt = await sync_to_async(setup_case)(case, iteration)
            try:
                await invoke(invocation_id)
                first = await sync_to_async(inspect_run)(user_id, course_id, run_id)
                expected = case[2]
                passed = first['course_present']
                if expected in ('question','resume'):
                    passed &= first['status'] == 'waiting' and bool(first['questions']) and not first['effects']
                    passed &= all(q['kind'] != 'confirmation' for q in first['questions'])
                    # Independently reject the unsafe alternatives observed in the
                    # first live run, even when the workflow correctly waits.
                    offered = json.dumps([q['question'].get('options',[]) for q in first['questions']], ensure_ascii=False)
                    if case[0] == 'alternate_conflict':
                        passed &= not any(t in offered for t in ('15h20','15:20'))
                    if case[0] == 'recurring_conflict':
                        passed &= not any(t in offered for t in ('12h40','12:40'))
                elif expected == 'create':
                    passed &= first['status'] == 'completed' and len(first['created_slots']) == 1
                    passed &= bool(first['created_slots']) and first['created_slots'][0]['end_time'] == walltime(13) and first['receipts'] == ['confirme']
                elif expected == 'correction':
                    passed &= first['status'] == 'completed' and not first['effects'] and not first['questions']
                elif expected == 'social':
                    passed &= first['status'] == 'completed' and not first['effects'] and not first['questions']
                else:
                    text = first['text'].casefold()
                    passed &= first['status'] == 'completed' and not first['effects']
                    passed &= any(word in text for word in ('transport','trajet'))
                final = None
                if expected == 'resume' and passed:
                    invocation_id = await sync_to_async(resume_case)(user_id, run_id, first['questions'][0])
                    await invoke(invocation_id)
                    final = await sync_to_async(inspect_run)(user_id, course_id, run_id)
                    passed &= final['status'] == 'completed' and len(final['created_slots']) == 1
                    passed &= bool(final['created_slots']) and final['created_slots'][0]['end_time'] == walltime(12,40)
                    passed &= final['receipts'] == ['confirme']
                row = dict(id=case[0], iteration=iteration, prompt=prompt, passed=bool(passed),
                           elapsed_s=round(time.monotonic()-started,2), first=first, final=final)
                rows.append(row)
                Path(options.output).write_text(json.dumps(dict(provider='DeepSeek', model='deepseek-flash',
                    transport='authenticated_ssh_buffered' if options.railway_relay else 'direct_https',
                    batch=batch, passed=sum(r['passed'] for r in rows), count=len(rows), results=rows),
                    ensure_ascii=False, indent=2, default=str), encoding='utf-8')
                print(f"{case[0]} {'PASS' if passed else 'FAIL'} {row['elapsed_s']}s", flush=True)
            finally:
                await sync_to_async(lambda: get_user_model().objects.filter(pk=user_id).delete())()
    return all(row['passed'] for row in rows)


DBOS(config={'name':f'constraints-{batch}','system_database_url':url,'enable_otlp':False,
             'application_version':f'constraints-{batch}','executor_id':f'constraints-{batch}','log_level':'ERROR'})
if options.railway_relay:
    from django.conf import settings
    from provider_ssh_relay import model
    configure_agent(model(settings.PLANNER_RUNTIME_MODEL))
else:
    configure_agent()
DBOS.launch()
try:
    passed = asyncio.run(main())
finally:
    DBOS.destroy()
raise SystemExit(0 if passed else 1)
