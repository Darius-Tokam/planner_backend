from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import AgentInteraction, AgentOperation, AgentRun, Conversation, Task
from services.agent_runtime import store


class PersistenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('runtime-owner')
        self.other = get_user_model().objects.create_user('runtime-other')
        self.conversation = Conversation.objects.create(user=self.user, engine_version='v3')
        self.run = AgentRun.objects.create(user=self.user, conversation=self.conversation,
                                          payload_hash='hash', prompt='bonjour')

    def test_event_replay_is_ordered_and_idempotent(self):
        one = store.emit(self.run.pk, 'text', {'text': 'Salut'}, key='first')
        replay = store.emit(self.run.pk, 'text', {'text': 'Salut'}, key='first')
        two = store.emit(self.run.pk, 'status', {'status': 'completed'}, key='last')
        self.assertEqual(one.pk, replay.pk)
        self.assertEqual([one.seq, two.seq], [1, 2])
        self.assertEqual(list(store.events(self.user, self.run.pk, after=1).values_list('seq', flat=True)), [2])
        with self.assertRaises(AgentRun.DoesNotExist):
            store.events(self.other, self.run.pk)

    def test_operation_receipt_and_effect_replayed_once(self):
        def write():
            task = Task.objects.create(user=self.user, title='Rapport')
            return {'success': True, 'data': {'task_id': task.pk}}
        first = store.commit_operation(self.run.pk, 'call1', 'create_task', {'title': 'Rapport'}, write)
        second = store.commit_operation(self.run.pk, 'call1', 'create_task', {'title': 'Rapport'}, write)
        self.assertEqual(first, second)
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(AgentOperation.objects.count(), 1)
        with self.assertRaises(store.Conflict):
            store.commit_operation(self.run.pk, 'call1', 'create_task', {'title': 'Autre'}, write)

    def test_exception_rolls_back_business_effect_and_returns_safe_result(self):
        def write():
            Task.objects.create(user=self.user, title='Annulé')
            raise ValueError('private provider message')
        result = store.commit_operation(self.run.pk, 'call1', 'create_task', {}, write)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(result['success'])
        self.assertEqual(result['cause'], 'tool_exception')
        self.assertNotIn('private', str(result))

    def interaction(self, kind='text'):
        return AgentInteraction.objects.create(run=self.run, tool_call_id='q1', kind=kind,
            question={'question': 'Quelle heure ?', 'kind': kind},
            expires_at=timezone.now() + timedelta(hours=24))

    def test_response_is_bound_to_owner_and_same_call(self):
        interaction = self.interaction()
        with self.assertRaises(AgentInteraction.DoesNotExist):
            store.answer(self.other, interaction.pk, {'text': '15:00 - 17:00'})
        store.answer(self.user, interaction.pk, {'text': '15:00 - 17:00'})
        interaction.refresh_from_db()
        self.assertEqual(interaction.answer, {'text': '15:00 - 17:00'})
        self.assertEqual(interaction.tool_call_id, 'q1')
        store.answer(self.user, interaction.pk, {'text': '15:00 - 17:00'})
        with self.assertRaises(store.Conflict):
            store.answer(self.user, interaction.pk, {'text': '18:00'})

    def test_invalid_or_expired_response_does_not_authorize(self):
        interaction = self.interaction('confirmation')
        with self.assertRaises(store.InvalidAnswer):
            store.answer(self.user, interaction.pk, {'approved': 'true'})
        interaction.expires_at = timezone.now() - timedelta(seconds=1)
        interaction.save()
        with self.assertRaises(store.Conflict):
            store.answer(self.user, interaction.pk, {'approved': True})
        interaction.refresh_from_db()
        self.assertEqual(interaction.status, 'pending')
