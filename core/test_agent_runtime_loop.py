import asyncio
import json

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ToolReturnPart, ModelMessagesTypeAdapter
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai import DeferredToolRequests

from core.models import AgentEvent, AgentInteraction, AgentInvocation, AgentRun, Conversation, Task
from services.agent_runtime import store
from services.agent_runtime.engine import Dependencies, build_agent
from services.agent_runtime.tools import resume_interaction


class LoopTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('loop-owner')
        self.user.profile.ai_consent_at = timezone.now()
        self.user.profile.save()
        conv = Conversation.objects.create(user=self.user, engine_version='v3')
        self.run = AgentRun.objects.create(user=self.user, conversation=conv, payload_hash='x')
        self.invocation = AgentInvocation.objects.create(run=self.run, number=0)

    def deps(self):
        return Dependencies(str(self.run.pk), str(self.invocation.pk))

    def test_text_is_persisted_before_generation_finishes(self):
        async def stream(messages, info):
            yield 'Bonjour'
            await asyncio.sleep(.05)
            visible = await sync_to_async(lambda: AgentEvent.objects.filter(run=self.run, kind='text').exists())()
            self.assertTrue(visible)
            yield ' !'
        agent = build_agent(FunctionModel(stream_function=stream))
        result = async_to_sync(agent.run)('Salut', deps=self.deps())
        self.assertEqual(result.output, 'Bonjour !')
        self.assertEqual(AgentEvent.objects.filter(kind='text').last().data['text'], 'Bonjour !')

    def test_question_and_mutation_in_same_response_waits_before_any_write(self):
        async def stream(messages, info):
            yield {
                0: DeltaToolCall(name='create_task', tool_call_id='premature', json_args='{"title":"Unconfirmed interpretation"}'),
                1: DeltaToolCall(name='ask_question_custom', tool_call_id='clarify', json_args='{"kind":"text","question":"Quelle action veux-tu ?"}'),
            }
        result = async_to_sync(build_agent(FunctionModel(stream_function=stream)).run)('Ambigu', deps=self.deps())
        self.assertIsInstance(result.output, DeferredToolRequests)
        self.assertFalse(Task.objects.filter(user=self.user).exists())

    def test_budget_interruption_has_precise_reason_and_reset(self):
        from services.agent_runtime.workflow import interruption_error
        from services.agent_v2.budget_tour import BudgetEpuise
        first = interruption_error(self.run.pk, BudgetEpuise('private', 'quota_mesure'))
        second = interruption_error(self.run.pk, BudgetEpuise('private', 'garde_cout'))
        self.assertEqual(first['code'], 'quota_mesure')
        self.assertNotEqual(first['message'], second['message'])
        self.assertIn('reprise_a', first)
        self.assertNotIn('private', str(first))

    def test_error_returns_to_model_then_corrected_tool_executes(self):
        calls = []
        async def stream(messages, info):
            calls.append(messages)
            if len(calls) == 1:
                yield {0: DeltaToolCall(name='create_task', json_args='{"title":"Réviser","priority":99}', tool_call_id='bad')}
            elif len(calls) == 2:
                returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
                self.assertEqual(returns[-1].content['cause'], 'invalid_arguments')
                yield {0: DeltaToolCall(name='create_task', json_args='{"title":"Réviser","priority":5}', tool_call_id='good')}
            else:
                returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
                self.assertEqual(returns[-1].content['receipt']['statut'], 'confirme')
                yield 'Ta tâche est créée.'
        result = async_to_sync(build_agent(FunctionModel(stream_function=stream)).run)('Crée Réviser', deps=self.deps())
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.output, 'Ta tâche est créée.')
        self.assertEqual(Task.objects.filter(user=self.user).count(), 1)

    def test_answer_resumes_same_tool_with_typed_history(self):
        calls = []
        async def stream(messages, info):
            calls.append(messages)
            if len(calls) == 1:
                yield {0: DeltaToolCall(name='ask_question_custom', tool_call_id='hours', json_args=json.dumps({
                    'kind': 'text', 'question': 'À quelles heures ?'}))}
            else:
                result = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][-1]
                self.assertEqual(result.tool_call_id, 'hours')
                self.assertEqual(result.content['data']['answer']['text'], '15:00 - 17:00')
                yield 'Je retiens 15 h à 17 h.'
        agent = build_agent(FunctionModel(stream_function=stream))
        result = async_to_sync(agent.run)('Examen lundi', deps=self.deps())
        self.assertIsInstance(result.output, DeferredToolRequests)
        saved = ModelMessagesTypeAdapter.validate_json(result.all_messages_json())
        interaction = AgentInteraction.objects.get(run=self.run)
        store.answer(self.user, interaction.pk, {'text': '15:00 - 17:00'})
        resumed = async_to_sync(agent.run)(None, deps=self.deps(), message_history=saved,
            deferred_tool_results=DeferredToolResults(calls={'hours': resume_interaction(interaction.pk)}))
        self.assertEqual(resumed.output, 'Je retiens 15 h à 17 h.')
        self.assertEqual(len(calls), 2)

    def test_loaded_tool_has_complete_schema_on_next_request(self):
        calls = []
        async def stream(messages, info):
            calls.append(messages)
            if len(calls) == 1:
                self.assertNotIn('optimize_week', [t.name for t in info.function_tools])
                yield {0: DeltaToolCall(name='load_tools', tool_call_id='load', json_args='{"names":["optimize_week"]}')}
            else:
                tool = next(t for t in info.function_tools if t.name == 'optimize_week')
                self.assertIn('apply', tool.parameters_json_schema['properties'])
                yield 'Prêt.'
        async_to_sync(build_agent(FunctionModel(stream_function=stream)).run)('Optimiser', deps=self.deps())
        self.assertEqual(len(calls), 2)

    def test_completed_provider_checkpoint_is_not_billed_twice(self):
        from services.agent_runtime.model import AccountedModel
        from core.models import AgentModelAttempt
        from services.agent_v2.budget_tour import statut_usage
        calls = []
        async def stream(messages, info):
            calls.append(1)
            yield 'Bonjour.'
        agent = build_agent(AccountedModel(FunctionModel(stream_function=stream)))
        async_to_sync(agent.run)('Salut', deps=self.deps())
        before = statut_usage(self.user)
        async_to_sync(agent.run)('Salut', deps=self.deps())
        self.assertEqual(statut_usage(self.user), before)
        self.assertEqual(len(calls), 1)
        self.assertEqual(AgentModelAttempt.objects.count(), 1)

    def test_loaded_tools_survive_human_pause_with_fresh_dependencies(self):
        calls = []
        async def stream(messages, info):
            calls.append(1)
            if len(calls) == 1:
                yield {0: DeltaToolCall(name='load_tools', tool_call_id='load', json_args='{"names":["optimize_week"]}')}
            elif len(calls) == 2:
                yield {0: DeltaToolCall(name='ask_question_custom', tool_call_id='scope', json_args='{"kind":"text","question":"Quelle semaine ?"}')}
            else:
                self.assertIn('optimize_week', [tool.name for tool in info.function_tools])
                yield 'La semaine indiquée.'
        agent = build_agent(FunctionModel(stream_function=stream))
        first = async_to_sync(agent.run)('Optimise', deps=self.deps())
        question = AgentInteraction.objects.get(run=self.run)
        store.answer(self.user, question.pk, {'text': 'Cette semaine'})
        async_to_sync(agent.run)(None, deps=self.deps(), message_history=first.all_messages(),
            deferred_tool_results=DeferredToolResults(calls={'scope': resume_interaction(question.pk)}))
