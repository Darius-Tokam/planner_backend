"""Run separately from Gunicorn. DBOS resumes committed workflows on restart."""
import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, close_old_connections


class Command(BaseCommand):
    help = 'Exécute les conversations v3 durables avec PostgreSQL et DBOS.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Traite les demandes en file puis quitte.')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('Le worker v3 exige PostgreSQL.')
        from dbos import DBOS, Queue, SetWorkflowID
        from core.models import AgentInvocation
        from services.agent_runtime.workflow import configure_agent, run_invocation
        from services.agent_runtime.admission import expire_waiting
        url = os.getenv('DBOS_SYSTEM_DATABASE_URL') or os.getenv('DATABASE_URL')
        if not url:
            raise CommandError('DATABASE_URL ou DBOS_SYSTEM_DATABASE_URL requis.')
        DBOS(config={'name': 'planner-v3', 'system_database_url': url,
                     'application_version': os.getenv('PLANNER_RUNTIME_VERSION', 'v3-1'),
                     'executor_id': os.getenv('PLANNER_WORKER_ID', 'planner-v3-worker'),
                     'enable_otlp': False})
        queue = Queue('planner-v3', concurrency=4, worker_concurrency=4)
        configure_agent()
        DBOS.launch()
        self.stdout.write('Worker Planner v3 démarré.')
        try:
            while True:
                close_old_connections()
                expire_waiting()
                ids = list(AgentInvocation.objects.filter(status='queued', run__status='queued')
                           .order_by('created_at').values_list('pk', flat=True)[:100])
                handles = []
                for invocation_id in ids:
                    with SetWorkflowID(f'planner-v3-{invocation_id}'):
                        handles.append(queue.enqueue(run_invocation, str(invocation_id)))
                if options['once']:
                    for handle in handles:
                        handle.get_result()
                    break
                time.sleep(.5)
        except KeyboardInterrupt:
            self.stdout.write('Arrêt du worker; les demandes restent persistées.')
        finally:
            DBOS.destroy()
