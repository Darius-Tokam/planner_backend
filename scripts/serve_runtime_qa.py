"""Local QA server/worker using isolated fixtures, never a deployed database."""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
url = os.environ.get('DATABASE_URL', '')
if urlparse(url).hostname not in ('127.0.0.1', 'localhost') or urlparse(url).path != '/planner_refonte':
    raise SystemExit('Isolated local database required.')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
os.environ['PLANNER_RUNTIME_DEFAULT'] = 'v3'
os.environ['CORS_ALLOWED_ORIGINS'] = 'http://127.0.0.1:5179'
mode = sys.argv[1]
if mode == 'worker':
    from dotenv import dotenv_values
    key = dotenv_values(root.parent / 'planner_backend/.env').get('DEEPSEEK_API_KEY')
    if key:
        os.environ.setdefault('DEEPSEEK_API_KEY', key)
import django
django.setup()
from django.core.management import call_command
if mode == 'seed':
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    username = os.getenv('PLANNER_QA_USER', 'runtime-qa')
    if not username.startswith('runtime-qa'):
        raise SystemExit('QA fixture username required.')
    user, _ = get_user_model().objects.get_or_create(username=username, defaults={'email': username + '@example.invalid'})
    user.set_password('Local-QA-only-2026!')
    user.save()
    user.profile.ai_consent_at = timezone.now()
    user.profile.onboarding_completed = True
    user.profile.save()
    print('Local QA account ready.')
elif mode == 'api':
    call_command('runserver', '127.0.0.1:8017', use_reloader=False)
elif mode == 'worker':
    if os.getenv('PLANNER_QA_CONTROLLED') == '1':
        from runtime_qa_model import model
        from services.agent_runtime.workflow import configure_agent
        configure_agent(model())
        print('QA: controlled provider, no semantic/provider performance claims.', flush=True)
    call_command('run_chat_worker')
else:
    raise SystemExit('Use seed, api or worker.')
