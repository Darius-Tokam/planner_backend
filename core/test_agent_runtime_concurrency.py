from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from unittest import skipUnless
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import AgentRun, AgentInteraction, Conversation, Task
from services.agent_runtime import store, admission


@skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locks')
class RuntimeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('concurrent-runtime')
        self.conv = Conversation.objects.create(user=self.user, engine_version='v3')
        self.run = admission.admit(self.user, self.conv.pk, uuid4(), 'Test', 'hash')

    def race(self, function, count=2):
        barrier = Barrier(count)
        def worker(index):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                return function(index)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(worker, range(count)))

    def test_concurrent_replay_has_one_domain_effect(self):
        def call(_):
            def effect():
                task = Task.objects.create(user_id=self.user.pk, title='Once')
                return {'success': True, 'data': {'id': task.pk}}
            return store.commit_operation(self.run.pk, 'same-call', 'create_task', {'title': 'Once'}, effect)
        results = self.race(call)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Task.objects.filter(title='Once').count(), 1)
        self.assertEqual(self.run.operations.count(), 1)

    def test_competing_answers_never_create_two_resumptions(self):
        self.run.status = 'waiting'
        self.run.save(update_fields=['status'])
        question = AgentInteraction.objects.create(run=self.run, kind='confirmation', tool_call_id='ask',
            question={'question': 'Confirmer ?'}, expires_at=timezone.now() + timedelta(hours=1))
        def call(index):
            try:
                admission.resume(self.user, question.pk, {'approved': bool(index)})
                return 'accepted'
            except store.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(call), ['accepted', 'conflict'])
        self.assertEqual(self.run.invocations.count(), 2)

    def test_two_admissions_have_one_active_owner(self):
        self.run.status = 'completed'
        self.run.save(update_fields=['status'])
        def call(index):
            try:
                admission.admit(self.user, self.conv.pk, uuid4(), f'Message {index}', str(index))
                return 'accepted'
            except store.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(call), ['accepted', 'conflict'])

    def test_concurrent_events_have_gapless_order(self):
        self.race(lambda i: store.emit(self.run.pk, 'text', {'part_id': str(i), 'text': str(i)}, key=f'part-{i}'), count=8)
        self.assertEqual(list(self.run.events.values_list('seq', flat=True)), list(range(1, 10)))
