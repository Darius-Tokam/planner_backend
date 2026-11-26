from datetime import timedelta
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import AgentInteraction, AgentInvocation, AgentRun, Conversation, ConversationMessage, UploadedDocument
from services.agent_runtime.store import emit


@override_settings(PLANNER_RUNTIME_DEFAULT='v3')
class RuntimeAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('api-runtime')
        self.other = get_user_model().objects.create_user('api-other')
        self.user.profile.ai_consent_at = timezone.now()
        self.user.profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.conv = Conversation.objects.create(user=self.user, engine_version='v3')

    def post(self, **extra):
        return self.client.post('/api/chat/runs/', {'conversation_id': self.conv.pk,
            'turn_id': str(uuid4()), 'message': 'Bonjour', **extra}, format='json')

    def test_admission_replay_and_payload_identity(self):
        turn = str(uuid4())
        first = self.post(turn_id=turn)
        self.assertEqual(first.status_code, 202)
        second = self.post(turn_id=turn)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.data['run_id'], second.data['run_id'])
        self.assertEqual(AgentInvocation.objects.count(), 1)
        self.assertEqual(ConversationMessage.objects.filter(role='user').count(), 1)
        self.assertEqual(self.post(turn_id=turn, message='Different').status_code, 409)

    def test_pending_run_survives_history_reload(self):
        run_id = self.post().data['run_id']
        response = self.client.get(f'/api/conversations/{self.conv.pk}/messages/')
        assistants = [m for m in response.data['messages'] if m['role'] == 'assistant']
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]['metadata']['run_id'], run_id)

    def test_legacy_routes_cannot_execute_a_v3_conversation(self):
        for path in ('/api/chat/', '/api/chat/stream/'):
            response = self.client.post(path, {'conversation_id': self.conv.pk, 'message': 'Bonjour'})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data['code'], 'runtime_required')

    def test_expired_question_releases_conversation_without_action(self):
        from services.agent_runtime.admission import expire_waiting
        run = AgentRun.objects.get(pk=self.post().data['run_id'])
        run.status = 'waiting'
        run.save()
        AgentInteraction.objects.create(run=run, tool_call_id='old', kind='text',
            question={'question': 'Quand ?'}, expires_at=timezone.now() - timedelta(seconds=1))
        expire_waiting()
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertFalse(run.operations.exists())
        self.assertEqual(self.post().status_code, 202)

    def test_owner_scope_and_consent(self):
        other_conv = Conversation.objects.create(user=self.other, engine_version='v3')
        self.assertEqual(self.post(conversation_id=other_conv.pk).status_code, 404)
        first = self.post()
        self.assertEqual(first.status_code, 202)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/chat/runs/{first.data['run_id']}/").status_code, 404)
        self.assertEqual(self.client.post('/api/chat/runs/', {}, format='json').status_code, 403)

    def test_one_active_run_and_v2_conversation_stays_v2(self):
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().status_code, 409)
        legacy = Conversation.objects.create(user=self.user)
        self.assertEqual(self.post(conversation_id=legacy.pk).status_code, 409)

    def test_snapshot_cursor_and_terminal_receipts(self):
        run_id = self.post().data['run_id']
        run = AgentRun.objects.get(pk=run_id)
        one = emit(run.pk, 'text', {'part_id': 'a', 'text': 'Bonjour'}, key='text')
        two = emit(run.pk, 'status', {'status': 'completed'}, key='complete')
        run.status = 'completed'
        run.save()
        response = self.client.get(f'/api/chat/runs/{run_id}/?after={one.seq}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e['seq'] for e in response.data['events']], [two.seq])
        self.assertEqual(response.data['status'], 'completed')

    def test_answer_validation_and_single_resumption(self):
        run = AgentRun.objects.get(pk=self.post().data['run_id'])
        run.status = 'waiting'
        run.save()
        run.invocations.update(status='waiting')
        interaction = AgentInteraction.objects.create(run=run, tool_call_id='hours', kind='text',
            question={'kind': 'text', 'question': 'Quelle heure ?'}, expires_at=timezone.now() + timedelta(hours=1))
        url = f'/api/chat/interactions/{interaction.pk}/answer/'
        self.assertEqual(self.client.post(url, {'answer': {'text': ''}}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'answer': {'text': '15:00 - 17:00'}}, format='json').status_code, 202)
        self.assertEqual(self.client.post(url, {'answer': {'text': '15:00 - 17:00'}}, format='json').status_code, 202)
        self.assertEqual(run.invocations.count(), 2)
        resumed = run.invocations.order_by('number').last()
        self.assertEqual(resumed.inputs['interactions'][0]['tool_call_id'], 'hours')

    def test_client_capability_required_for_new_v3_conversation(self):
        old = self.client.post('/api/conversations/', {}, format='json')
        new = self.client.post('/api/conversations/', {'client_runtime': 'v3'}, format='json')
        self.assertEqual(old.data['engine_version'], 'v2')
        self.assertEqual(new.data['engine_version'], 'v3')

    def test_invalid_identifier_is_400_and_stream_replays_after_cursor(self):
        self.assertEqual(self.post(turn_id='not-a-uuid').status_code, 400)
        run = AgentRun.objects.get(pk=self.post().data['run_id'])
        first = emit(run.pk, 'text', {'part_id': 'p', 'text': 'Début'}, key='a')
        last = emit(run.pk, 'text', {'part_id': 'p', 'text': 'Début et fin'}, key='b')
        run.status = 'completed'
        run.save()
        response = self.client.get(f'/api/chat/runs/{run.pk}/events/?after={first.seq}')
        body = b''.join(response.streaming_content).decode()
        self.assertIn(f'id: {last.seq}', body)
        self.assertNotIn(f'id: {first.seq}\n', body)

    def test_cancel_preserves_receipts_and_prevents_new_tool_effects(self):
        run = AgentRun.objects.get(pk=self.post().data['run_id'])
        response = self.client.delete(f'/api/chat/runs/{run.pk}/')
        self.assertEqual(response.data['status'], 'cancelled')
        from services.agent_runtime.tools import execute_call
        result = execute_call(run.pk, 'late', 'create_task', {'title': 'Too late'})
        self.assertEqual(result['cause'], 'run_cancelled')

    def test_uploaded_documents_are_saved_once_and_queued(self):
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            turn = str(uuid4())
            def request():
                return self.client.post('/api/chat/runs/', {'conversation_id': self.conv.pk,
                    'turn_id': turn, 'attachment': [SimpleUploadedFile('one.pdf', b'%PDF-1.4\nabc'),
                    SimpleUploadedFile('two.pdf', b'%PDF-1.4\ndef')]}, format='multipart')
            self.assertEqual(request().status_code, 202)
            self.assertEqual(request().status_code, 202)
            self.assertEqual(UploadedDocument.objects.count(), 2)
            self.assertEqual(ConversationMessage.objects.get(role='user').attachments.count(), 2)
