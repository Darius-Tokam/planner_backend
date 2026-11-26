from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import AgentInteraction, AgentRun, Conversation, Task
from services.agent_runtime import store
from services.agent_runtime.tools import execute_call, resume_interaction, schema_for


class RuntimeToolsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('tool-owner')
        self.user.profile.ai_consent_at = timezone.now()
        self.user.profile.save()
        self.other = get_user_model().objects.create_user('tool-other')
        self.conversation = Conversation.objects.create(user=self.user, engine_version='v3')
        self.run = AgentRun.objects.create(user=self.user, conversation=self.conversation, payload_hash='x')
        self.task = Task.objects.create(user=self.user, title='Rapport')

    def test_creation_verifies_and_replays_receipt(self):
        result = execute_call(self.run.pk, 'c1', 'create_task', {'title': 'Réviser'})
        self.assertEqual(result['receipt']['statut'], 'confirme')
        again = execute_call(self.run.pk, 'c1', 'create_task', {'title': 'Réviser'})
        self.assertEqual(result, again)
        self.assertEqual(Task.objects.filter(title='Réviser').count(), 1)

    def test_typed_recurrence_dates_are_passed_to_domain_tool(self):
        from datetime import timedelta
        result = execute_call(self.run.pk, 'series', 'manage_recurrence', {
            'action': 'create', 'kind': 'task', 'title': 'Poubelles',
            'start_date': (timezone.localdate() + timedelta(days=1)).isoformat(),
            'frequency': 'weekly', 'weekdays': [1], 'due_time': '20:00'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['receipt']['statut'], 'confirme')

    def test_new_request_can_create_same_title_intentionally(self):
        execute_call(self.run.pk, 'c1', 'create_task', {'title': 'Réviser'})
        other_run = AgentRun.objects.create(user=self.user, conversation=self.conversation, payload_hash='y')
        execute_call(other_run.pk, 'c2', 'create_task', {'title': 'Réviser'})
        self.assertEqual(Task.objects.filter(title='Réviser').count(), 2)

    def test_confirmation_cannot_be_forged_by_model(self):
        result = execute_call(self.run.pk, 'd1', 'delete_task', {'task_id': self.task.pk, 'confirm': True})
        self.assertEqual(result['status'], 'waiting')
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())
        self.assertNotIn('confirm', schema_for('delete_task')['properties'])

    def test_deleting_a_recurring_block_requires_confirmation(self):
        from core.models import RecurringBlock
        block = RecurringBlock.objects.create(user=self.user, title='Sport', day_of_week=0,
            start_time='18:00', end_time='19:00', block_type='sport')
        result = execute_call(self.run.pk, 'delete-block', 'delete_block', {'block_id': block.pk})
        self.assertEqual(result['status'], 'waiting')
        block.refresh_from_db()
        self.assertTrue(block.active)

    def test_form_validation_is_active_under_python_optimization(self):
        import inspect
        from services.agent_runtime import questions
        namespace = dict(vars(questions))
        exec(compile(inspect.getsource(questions.validate_form_answer), '<optimized-validator>', 'exec', optimize=2), namespace)
        with self.assertRaises(store.InvalidAnswer):
            namespace['validate_form_answer']({'fields': [{'id': 'x', 'type': 'radio',
                'label': 'Choix', 'options': [{'value': 'a'}, {'value': 'b'}]}]}, {'fields': {'x': 'forged'}})

    def test_approval_applies_only_same_target_and_receipt(self):
        execute_call(self.run.pk, 'd1', 'delete_task', {'task_id': self.task.pk})
        question = AgentInteraction.objects.get(run=self.run)
        store.answer(self.user, question.pk, {'approved': True})
        result = resume_interaction(question.pk)
        self.assertEqual(result['receipt']['statut'], 'confirme')
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertEqual(result, resume_interaction(question.pk))

    def test_changed_target_requires_fresh_authorization(self):
        execute_call(self.run.pk, 'd1', 'delete_task', {'task_id': self.task.pk})
        question = AgentInteraction.objects.get(run=self.run)
        Task.objects.filter(pk=self.task.pk).update(title='Changed by another client')
        store.answer(self.user, question.pk, {'approved': True})
        self.assertEqual(resume_interaction(question.pk)['cause'], 'target_changed')
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def test_refusal_returns_to_model_and_blocks_same_action(self):
        execute_call(self.run.pk, 'd1', 'delete_task', {'task_id': self.task.pk})
        question = AgentInteraction.objects.get(run=self.run)
        store.answer(self.user, question.pk, {'approved': False})
        self.assertEqual(resume_interaction(question.pk)['cause'], 'user_declined')
        repeated = execute_call(self.run.pk, 'd2', 'delete_task', {'task_id': self.task.pk})
        self.assertEqual(repeated['cause'], 'user_declined')
        self.assertEqual(AgentInteraction.objects.filter(run=self.run).count(), 1)

    def test_other_user_and_private_arguments_rejected(self):
        other_task = Task.objects.create(user=self.other, title='Private')
        result = execute_call(self.run.pk, 'u1', 'update_task', {'task_id': other_task.pk, 'title': 'x'})
        self.assertFalse(result['success'])
        result = execute_call(self.run.pk, 'u2', 'create_task', {'title': 'x', '_confirmed': True})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertFalse(Task.objects.filter(user=self.user, title='x').exists())

    def test_error_and_null_data_are_preserved(self):
        result = execute_call(self.run.pk, 'r1', 'list_tasks', {})
        self.assertIsNone(result['data']['tasks'][0]['deadline'])
        result = execute_call(self.run.pk, 'bad', 'create_task', {'title': 'x', 'priority': 99})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertIn('priority', str(result['data']))

    def test_task_read_by_id_is_scoped_and_pagination_is_explicit(self):
        own = execute_call(self.run.pk, 'own-read', 'list_tasks', {'task_id': self.task.pk})
        self.assertTrue(own['success'])
        self.assertEqual(own['data']['tasks'][0]['id'], self.task.pk)
        foreign = Task.objects.create(user=self.other, title='Private')
        hidden = execute_call(self.run.pk, 'foreign-read', 'list_tasks', {'task_id': foreign.pk})
        self.assertEqual(hidden['cause'], 'target_not_found')
        self.assertNotIn('Private', str(hidden))
        Task.objects.create(user=self.user, title='Second')
        first = execute_call(self.run.pk, 'page-one', 'list_tasks', {'limit': 1})['data']
        self.assertTrue(first['truncated'])
        second = execute_call(self.run.pk, 'page-two', 'list_tasks', {'limit': 1, 'offset': first['next_offset']})['data']
        self.assertFalse(second['truncated'])
        self.assertNotEqual(first['tasks'][0]['id'], second['tasks'][0]['id'])

    def test_question_form_keeps_other_and_validates_fields(self):
        result = execute_call(self.run.pk, 'q1', 'ask_question_custom', {
            'kind': 'form', 'question': 'Quel horaire ?', 'fields': [
                {'id': 'hours', 'type': 'text', 'label': 'Heures'},
                {'id': 'place', 'type': 'radio', 'label': 'Lieu', 'allow_other': True,
                 'options': [{'value': 'school', 'label': 'École'}, {'value': 'home', 'label': 'Maison'}]}]})
        self.assertEqual(result['status'], 'waiting')
        question = AgentInteraction.objects.get(run=self.run)
        with self.assertRaises(store.InvalidAnswer):
            store.answer(self.user, question.pk, {'fields': {'hours': '', 'place': 'school'}})
        value = {'fields': {'hours': '15:00 - 17:00', 'place': {'other': 'Bibliothèque'}}}
        store.answer(self.user, question.pk, value)
        self.assertEqual(resume_interaction(question.pk)['data']['answer'], value)

    def test_free_answer_to_clarification_never_becomes_authorization(self):
        execute_call(self.run.pk, 'free', 'ask_question_custom', {
            'kind': 'form', 'question': 'Quand ?', 'fields': [{'id': 'date', 'type': 'date', 'label': 'Date'}]})
        question = AgentInteraction.objects.get(run=self.run, tool_call_id='free')
        store.answer(self.user, question.pk, {'text': 'La date est déjà indiquée; de 15:00 à 17:00.'})
        self.assertIn('15:00', resume_interaction(question.pk)['data']['answer']['text'])
        execute_call(self.run.pk, 'delete-free', 'delete_task', {'task_id': self.task.pk})
        confirmation = AgentInteraction.objects.get(run=self.run, tool_call_id='delete-free')
        with self.assertRaises(store.InvalidAnswer):
            store.answer(self.user, confirmation.pk, {'text': 'oui'})

    @patch('services.notification_delivery.has_device', return_value=False)
    def test_activation_is_only_proposed_for_an_actual_alert(self, _):
        untimed = execute_call(self.run.pk, 'untimed', 'create_task', {'title': 'Read'})
        self.assertFalse(untimed.get('notification_activation_required', False))
        timed = execute_call(self.run.pk, 'timed', 'create_task', {'title': 'Call', 'deadline': '2030-01-01T17:00:00-05:00'})
        self.assertTrue(timed['notification_activation_required'])

    @patch('services.notification_delivery.has_device', return_value=False)
    def test_missing_push_subscription_returns_activation_signal(self, _):
        result = execute_call(self.run.pk, 'push', 'send_notification', {'body': 'Test'})
        self.assertFalse(result['success'])
        self.assertEqual(result['cause'], 'aucun_appareil')
        self.assertTrue(result['notification_activation_required'])

    @patch('services.notification_delivery.has_device', return_value=True)
    @patch('services.notification_delivery.send_visible', return_value=1)
    def test_external_delivery_replay_never_sends_twice(self, send, _):
        first = execute_call(self.run.pk, 'push', 'send_notification', {'body': 'Test'})
        second = execute_call(self.run.pk, 'push', 'send_notification', {'body': 'Test'})
        self.assertEqual(first, second)
        self.assertEqual(first['receipt']['statut'], 'accepted')
        send.assert_called_once()

    @patch('services.notification_delivery.has_device', return_value=True)
    @patch('services.notification_delivery.send_visible')
    def test_unknown_delivery_never_retries(self, send, _):
        from services.agent_runtime.tools import _execute_call_db
        from core.models import AgentDelivery
        pending = _execute_call_db(self.run.pk, 'push', 'send_notification', {'body': 'Test'})
        AgentDelivery.objects.filter(pk=pending['operation_id']).update(status='sending')
        result = execute_call(self.run.pk, 'push', 'send_notification', {'body': 'Test'})
        self.assertEqual(result['cause'], 'delivery_unknown')
        send.assert_not_called()
