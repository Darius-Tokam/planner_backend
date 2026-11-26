"""Reproductions of the independent whole-branch review."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import (AgentRun, AgentInvocation, AgentInteraction, AgentOperation,
                         Conversation, ConversationMessage, Task, ScheduledBlock, RecurringBlock)
from services.agent_runtime import store, admission
from services.agent_runtime.tools import execute_call, resume_interaction


class RuntimeReviewTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('review')
        self.user.profile.ai_consent_at = timezone.now()
        self.user.profile.save()
        self.conv = Conversation.objects.create(user=self.user, engine_version='v3')
        self.run = AgentRun.objects.create(user=self.user, conversation=self.conv, payload_hash='x',
            status='running', context={'date': '2030-01-07'})
        self.invocation = AgentInvocation.objects.create(run=self.run, number=0, status='running')

    def test_answer_before_waiting_transition_queues_exactly_one_resume(self):
        from services.agent_runtime.workflow import finish
        execute_call(self.run.pk, 'q', 'ask_question_custom', {'kind': 'text', 'question': 'Quand ?'})
        interaction = AgentInteraction.objects.get(run=self.run)
        admission.resume(self.user, interaction.pk, {'text': '15:00'})
        finish(self.invocation.pk, [], 'waiting', pending_calls=['q'])
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'queued')
        admission.resume(self.user, interaction.pk, {'text': '15:00'})
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'queued')
        self.assertEqual(self.run.invocations.count(), 2)

    def test_delete_task_confirmation_includes_cascaded_children(self):
        task = Task.objects.create(user=self.user, title='Rapport')
        execute_call(self.run.pk, 'delete', 'delete_task', {'task_id': task.pk})
        question = AgentInteraction.objects.get(run=self.run)
        ScheduledBlock.objects.create(user=self.user, task=task, date=timezone.localdate(), start_time='15:00', end_time='16:00')
        store.answer(self.user, question.pk, {'approved': True})
        self.assertEqual(resume_interaction(question.pk)['cause'], 'target_changed')
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())

    def test_occurrence_confirmation_cannot_switch_to_replacement_block(self):
        date = timezone.localdate() + timedelta(days=1)
        old = RecurringBlock.objects.create(user=self.user, title='Sport', day_of_week=date.weekday(), start_time='15:00', end_time='16:00', block_type='sport')
        execute_call(self.run.pk, 'skip', 'skip_block_occurrence', {'date': date.isoformat(), 'title': 'Sport'})
        question = AgentInteraction.objects.get(run=self.run)
        old.active = False
        old.save(update_fields=['active'])
        RecurringBlock.objects.create(user=self.user, title='Sport', day_of_week=date.weekday(), start_time='15:00', end_time='16:00', block_type='sport')
        store.answer(self.user, question.pk, {'approved': True})
        self.assertEqual(resume_interaction(question.pk)['cause'], 'target_changed')
        from core.models import RecurringBlockException
        self.assertFalse(RecurringBlockException.objects.filter(user=self.user).exists())

    def test_default_optimization_date_is_frozen_before_confirmation(self):
        execute_call(self.run.pk, 'opt', 'optimize_week', {'apply': True})
        question = AgentInteraction.objects.get(run=self.run)
        self.assertEqual(question.prepared['arguments']['start_date'], '2030-01-07')

    def test_deferred_call_identity_cannot_change_tool_or_arguments(self):
        first = execute_call(self.run.pk, 'same', 'ask_question_custom', {'kind': 'text', 'question': 'Quand ?'})
        changed = execute_call(self.run.pk, 'same', 'ask_question_custom', {'kind': 'text', 'question': 'Où ?'})
        self.assertEqual(changed['cause'], 'call_identity_conflict')
        mutation = execute_call(self.run.pk, 'same', 'create_task', {'title': 'Unrelated'})
        self.assertEqual(mutation['cause'], 'call_identity_conflict')
        self.assertFalse(Task.objects.filter(user=self.user).exists())
        self.assertEqual(first['status'], 'waiting')

    def test_confirmation_never_consumes_another_operation_receipt(self):
        task = Task.objects.create(user=self.user, title='Rapport')
        execute_call(self.run.pk, 'call', 'delete_task', {'task_id': task.pk})
        question = AgentInteraction.objects.get(run=self.run)
        store.answer(self.user, question.pk, {'approved': True})
        store.commit_operation(self.run.pk, 'call', 'create_task', {'title': 'Other'}, lambda: {'success': True})
        self.assertEqual(resume_interaction(question.pk)['cause'], 'call_identity_conflict')
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())

    def test_invalid_result_reference_returns_tool_error(self):
        from services.agent_runtime.engine import _read_result
        result = _read_result(self.run.pk, 'not-a-uuid', [], 0, 20)
        self.assertFalse(result['success'])

    def test_result_root_and_nested_values_remain_bounded(self):
        import json
        from services.agent_runtime.engine import _read_result
        op = store.commit_operation(self.run.pk, 'huge', 'list_tasks', {},
            lambda: {'success': True, 'data': {'huge': 'x' * 100000, 'other': 'value'}})
        result = _read_result(self.run.pk, op['operation_id'], [], 0, 1)
        self.assertLessEqual(len(json.dumps(result)), 16000)
        self.assertTrue(result['truncated'])
        chunk = _read_result(self.run.pk, op['operation_id'], ['huge'], 10, 5)
        self.assertEqual(chunk['data'], 'xxxxx')

    def test_oversized_completed_turn_has_a_retrievable_reference(self):
        self.run.status = 'completed'
        self.run.prompt = 'Correction importante : mon rendez-vous est mardi.'
        self.run.result = {'message': 'x' * 60000}
        self.run.messages = [{'kind': 'request', 'parts': [{'part_kind': 'user-prompt', 'content': self.run.prompt}]},
                             {'kind': 'response', 'parts': [{'part_kind': 'text', 'content': 'x' * 60000}]}]
        self.run.save()
        next_run = admission.admit(self.user, self.conv.pk, uuid4(), 'Et sa durée ?', 'next')
        self.assertEqual(next_run.context['recent_turns'][0]['run_id'], str(self.run.pk))
        from services.agent_runtime.engine import _read_turn
        self.assertIn('mardi', _read_turn(next_run.pk, str(self.run.pk), 'prompt', 0, 1000)['data'])

    def test_attempt_and_reservation_are_admitted_atomically(self):
        from services.agent_runtime.model import start_attempt
        from core.models import ReservationBudgetJetons
        with patch('services.agent_runtime.model.AgentModelAttempt.objects.create', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                start_attempt(self.invocation.pk, 'req', 'planning', 'test', 1000, 100)
        self.assertEqual(ReservationBudgetJetons.objects.count(), 0)

    def test_completed_attempt_cannot_commit_without_closing_reservation(self):
        from services.agent_runtime.model import start_attempt, finish_attempt
        from core.models import AgentModelAttempt
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.usage import RequestUsage
        attempt, trace, call = start_attempt(self.invocation.pk, 'req', 'planning', 'test', 1000, 100)
        response = ModelResponse(parts=[TextPart('Done')], usage=RequestUsage(input_tokens=10, output_tokens=2))
        with patch('services.agent_runtime.model.fermer_trace', side_effect=RuntimeError('close failed')):
            with self.assertRaises(RuntimeError):
                finish_attempt(attempt.pk, trace, call, response, 10)
        self.assertEqual(AgentModelAttempt.objects.get(pk=attempt.pk).status, 'started')

    def test_account_deletion_keeps_a_durable_dbos_purge_request(self):
        from core.models import AgentWorkflowPurge
        invocation_id = self.invocation.pk
        self.user.delete()
        self.assertTrue(AgentWorkflowPurge.objects.filter(workflow_id=f'planner-v3-{invocation_id}').exists())

    def test_history_returns_latest_active_question_beyond_two_hundred_messages(self):
        from rest_framework.test import APIClient
        ConversationMessage.objects.bulk_create([ConversationMessage(user=self.user, conversation=self.conv,
            role='user', content=f'old {i}') for i in range(205)])
        last = ConversationMessage.objects.create(user=self.user, conversation=self.conv, role='assistant',
            content='Question active', metadata={'engine_version': 'v3', 'run_id': str(self.run.pk)})
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f'/api/conversations/{self.conv.pk}/messages/')
        self.assertIn(last.pk, [item['id'] for item in response.data['messages']])

    def test_document_extraction_does_not_change_the_planning(self):
        from services.agent_runtime.documents import ExtractionProcessor
        from core.models import UploadedDocument
        doc = UploadedDocument.objects.create(user=self.user, file='review.pdf', file_name='review.pdf')
        data = {'events': [{'title': 'Examen', 'date': '2030-01-10', 'start_time': '15:00', 'end_time': '17:00'}]}
        processor = ExtractionProcessor(self.run.pk)
        processor._materialize_document(doc, data)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(ScheduledBlock.objects.exists())
        doc.refresh_from_db()
        self.assertTrue(doc.processed)
        result = execute_call(self.run.pk, 'import', 'import_document', {'document_id': doc.pk})
        self.assertTrue(result['success'], result)
        self.assertEqual(ScheduledBlock.objects.count(), 1)
        execute_call(self.run.pk, 'import', 'import_document', {'document_id': doc.pk})
        self.assertEqual(ScheduledBlock.objects.count(), 1)

    def test_document_commit_rechecks_consent_after_extraction(self):
        from services.agent_runtime.documents import ExtractionProcessor
        from core.models import UploadedDocument
        doc = UploadedDocument.objects.create(user=self.user, file='review.pdf')
        self.user.profile.ai_consent_at = None
        self.user.profile.save()
        with self.assertRaises(PermissionError):
            ExtractionProcessor(self.run.pk)._materialize_document(doc, {'events': []})
        doc.refresh_from_db()
        self.assertFalse(doc.processed)

    def test_purge_failure_retains_retry_and_child_cleanup(self):
        from core.models import AgentWorkflowPurge
        from services.agent_runtime.purge import drain
        from types import SimpleNamespace
        row = AgentWorkflowPurge.objects.create(workflow_id='planner-v3-test')
        with patch('dbos.DBOS.list_workflows', return_value=[SimpleNamespace(workflow_id='planner-v3-test-1')]), \
             patch('dbos.DBOS.cancel_workflows'), patch('dbos.DBOS.delete_workflows', side_effect=RuntimeError):
            drain()
        row.refresh_from_db()
        self.assertEqual(row.last_error, 'RuntimeError')
        self.assertGreater(row.next_attempt_at, timezone.now())
