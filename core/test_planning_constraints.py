"""Public planning tools must explain the actual constraint, before any write."""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase

from core.models import RecurringBlock, RecurringBlockException, RecurringBlockOverride, ScheduledBlock, Task, UserPlace
from services.agent.tools import (schedule_task_at, get_today_schedule, get_week_schedule,
    find_free_slots, check_feasibility, organize_day, optimize_week, detect_conflicts,
    create_block, update_block, replace_block_occurrence, restore_block_occurrence)
from services.scheduling.constraints import fixed_constraints, day_constraints
from services.scheduling.placement import fixed_busy_intervals, occupied_intervals, _merge_intervals


class PlanningConstraintTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('constraint-reader')
        self.day = date(2030, 11, 28)
        self.user.profile.transport_time_minutes = 20
        self.user.profile.save()
        self.course = RecurringBlock.objects.create(
            user=self.user, title='Cours', block_type='course',
            day_of_week=self.day.weekday(), start_time=time(13), end_time=time(15),
            flexibility='fixed',
        )
        task = Task.objects.create(user=self.user, title='Révision')
        ScheduledBlock.objects.create(user=self.user, task=task, date=self.day,
                                      start_time=time(15, 20), end_time=time(16, 20))

    def test_transport_only_conflict_preserves_source_and_overlap(self):
        result = schedule_task_at.execute(self.user, title='Sortie',
            date=self.day.isoformat(), start_time='11:40', end_time='13:00')
        self.assertFalse(result.success)
        self.assertEqual(result.cause, 'planning_conflict')
        self.assertEqual(len(result.data['conflicts']), 1)
        conflict = result.data['conflicts'][0]
        self.assertEqual(conflict['kind'], 'transport')
        self.assertEqual(conflict['source']['id'], self.course.pk)
        self.assertEqual(conflict['source']['start_time'], '13:00')
        self.assertEqual(conflict['start_time'], '12:40')
        self.assertEqual(conflict['end_time'], '13:00')
        self.assertEqual(conflict['overlap_minutes'], 20)
        self.assertIn('transport', result.message.lower())
        self.assertFalse(Task.objects.filter(user=self.user, title='Sortie').exists())

    def test_touching_course_without_transport_is_allowed(self):
        self.user.profile.transport_time_minutes = 0
        self.user.profile.save()
        result = schedule_task_at.execute(self.user, title='Sortie',
            date=self.day.isoformat(), start_time='11:40', end_time='13:00')
        self.assertTrue(result.success, result.message)

    def test_all_day_readers_explain_the_same_transport(self):
        for tool, extra in ((get_today_schedule, {}), (find_free_slots, {}),
                            (check_feasibility, {'activities': [{'title': 'A', 'duration_minutes': 30}]}),
                            (organize_day, {'apply': False})):
            with self.subTest(tool=tool.NAME):
                result = tool.execute(self.user, date=self.day.isoformat(), **extra)
                self.assertTrue(result.success, result.message)
                parts = result.data['constraints']
                self.assertTrue(any(c['kind'] == 'transport' and c['start_time'] == '12:40'
                    and c['source']['id'] == self.course.pk for c in parts))
        today = get_today_schedule.execute(self.user, date=self.day.isoformat()).data
        slots = find_free_slots.execute(self.user, date=self.day.isoformat()).data
        self.assertEqual(today['free_slots'], slots['free_slots'])

    def test_week_readers_and_solver_preserve_margin_sources(self):
        for tool, key in ((get_week_schedule, 'reserved_margins'), (optimize_week, 'constraints')):
            with self.subTest(tool=tool.NAME):
                result = tool.execute(self.user, start_date=self.day.isoformat(), apply=False)
                self.assertTrue(result.success, result.message)
                self.assertTrue(any(c['kind'] == 'transport' for c in result.data['days'][0][key]))

    def test_failed_upsert_preserves_original_slot(self):
        original = schedule_task_at.execute(self.user, title='Sortie', date=self.day.isoformat(),
                                            start_time='11:40', end_time='12:40')
        self.assertTrue(original.success)
        result = schedule_task_at.execute(self.user, title='Sortie', date=self.day.isoformat(),
                                          start_time='11:40', end_time='13:00')
        self.assertFalse(result.success)
        block = ScheduledBlock.objects.get(pk=original.data['scheduled_block']['id'])
        self.assertEqual(block.end_time, time(12, 40))
        self.assertEqual(Task.objects.filter(user=self.user, title='Sortie').count(), 1)

    def test_completion_and_skip_remove_sources(self):
        revision = Task.objects.get(user=self.user, title='Révision')
        revision.completed = True
        revision.save()
        RecurringBlockException.objects.create(user=self.user, recurring_block=self.course, date=self.day)
        self.assertEqual(fixed_constraints(self.user, self.day), [])

    def test_places_explain_preparation_travel_and_safety_separately(self):
        self.course.place = UserPlace.objects.create(user=self.user, name='Campus', travel_minutes=32)
        self.course.save()
        self.user.profile.prep_time_minutes = 15
        self.user.profile.safety_margin_minutes = 10
        self.user.profile.save()
        parts = [c for c in fixed_constraints(self.user, self.day) if c.source['id'] == self.course.pk
                 and c.source['type'] == 'recurring' and c.direction == 'before']
        self.assertEqual([(c.kind, c.start, c.end) for c in parts],
                         [('preparation', 723, 738), ('transport', 738, 770), ('safety_margin', 770, 780)])
        self.assertTrue(all(c.policy == 'place' for c in parts))

    def test_conflicts_are_exhaustive_and_survive_deleting_only_one_source(self):
        other = Task.objects.create(user=self.user, title='Appel')
        sb = ScheduledBlock.objects.create(user=self.user, task=other, date=self.day,
                                           start_time=time(11, 40), end_time=time(12))
        args = dict(title='Sortie', date=self.day.isoformat(), start_time='11:40', end_time='13:00')
        first = schedule_task_at.execute(self.user, **args)
        self.assertTrue(first.data['conflict_is_exhaustive'])
        self.assertEqual({c['kind'] for c in first.data['conflicts']}, {'activity', 'transport'})
        sb.delete()
        second = schedule_task_at.execute(self.user, **args)
        self.assertFalse(second.success)
        self.assertEqual([c['kind'] for c in second.data['conflicts']], ['transport'])

    def test_detector_includes_task_against_transport_and_preserves_identity(self):
        task = Task.objects.create(user=self.user, title='Ancienne sortie')
        ScheduledBlock.objects.create(user=self.user, task=task, date=self.day,
                                      start_time=time(12, 45), end_time=time(13))
        result = detect_conflicts.execute(self.user, start_date=self.day.isoformat())
        self.assertTrue(any(any(c['kind'] == 'transport' for c in x['constraints'])
                            for x in result.data['conflicts']))
        self.assertIn('transport', result.public)

    def test_two_touching_activities_with_zero_transport_are_not_detected(self):
        self.user.profile.transport_time_minutes = 0
        self.user.profile.save()
        task = Task.objects.create(user=self.user, title='Sortie')
        ScheduledBlock.objects.create(user=self.user, task=task, date=self.day,
                                      start_time=time(12), end_time=time(13))
        self.assertEqual(detect_conflicts.execute(self.user, start_date=self.day.isoformat()).data['count'], 0)

    def test_recurring_creation_and_move_explain_transport_conflict(self):
        result = create_block.execute(self.user, title='Autre', block_type='work', days=[self.day.weekday()],
                                      start_time='11:40', end_time='13:00', start_date=self.day.isoformat())
        self.assertFalse(result.success, result.message)
        self.assertTrue(any(c['kind'] == 'transport' for c in result.data['conflicts']))
        block = RecurringBlock.objects.create(user=self.user, title='Autre', block_type='work',
            day_of_week=self.day.weekday(), start_time=time(18), end_time=time(19), flexibility='fixed')
        result = update_block.execute(self.user, block_id=block.pk, start_time='11:40', end_time='13:00')
        self.assertFalse(result.success)
        self.assertTrue(any(c['kind'] == 'transport' for c in result.data['conflicts']))
        block.refresh_from_db()
        self.assertEqual(block.start_time, time(18))

    def test_recurring_disjoint_date_windows_do_not_conflict(self):
        self.course.end_date = self.day
        self.course.save()
        result = create_block.execute(self.user, title='Session suivante', block_type='course',
            days=[self.day.weekday()], start_time='13:00', end_time='15:00',
            start_date=(self.day + timedelta(days=7)).isoformat())
        self.assertTrue(result.success, result.message)

    def test_replace_failure_rolls_back_skip_and_keeps_exact_cause(self):
        block = RecurringBlock.objects.create(user=self.user, title='Autre', block_type='work',
            day_of_week=self.day.weekday(), start_time=time(10), end_time=time(11), flexibility='fixed')
        result = replace_block_occurrence.execute(self.user, date=self.day.isoformat(), title='Autre',
            replacement_title='Sortie', start_time='11:40', end_time='13:00')
        self.assertFalse(result.success)
        self.assertEqual(result.data['conflicts'][0]['kind'], 'transport')
        self.assertFalse(RecurringBlockException.objects.filter(recurring_block=block).exists())

    def test_next_day_transport_and_previous_day_tail_are_visible(self):
        self.course.day_of_week = (self.day.weekday() + 1) % 7
        self.course.start_time, self.course.end_time = time(0, 10), time(1)
        self.course.save()
        hits = [c for c in fixed_constraints(self.user, self.day) if c.kind == 'transport']
        self.assertTrue(any((c.start, c.end) == (1430, 1440) for c in hits))
        self.course.day_of_week = (self.day.weekday() - 1) % 7
        self.course.start_time, self.course.end_time = time(22), time(0, 10)
        self.course.save()
        hits = [c for c in fixed_constraints(self.user, self.day) if c.kind == 'transport']
        self.assertTrue(any((c.start, c.end) == (10, 30) for c in hits))

    def test_projection_matches_constraints_and_does_not_leak_other_users(self):
        other = get_user_model().objects.create_user('other-constraint-owner')
        RecurringBlock.objects.create(user=other, title='SECRET TITLE', block_type='work',
            day_of_week=self.day.weekday(), start_time=time(9), end_time=time(22))
        self.assertNotIn('SECRET TITLE', str([c.as_dict() for c in day_constraints(self.user, self.day)]))
        for day in (self.day - timedelta(days=1), self.day, self.day + timedelta(days=1)):
            self.assertEqual(fixed_busy_intervals(self.user, day),
                             _merge_intervals([(c.start, c.end) for c in fixed_constraints(self.user, day)]))
            self.assertEqual(occupied_intervals(self.user, day),
                             _merge_intervals([(c.start, c.end) for c in day_constraints(self.user, day)]))

    def test_recurring_creation_respects_dated_task_and_its_bounds(self):
        args = dict(title='Travail', block_type='work', days=[self.day.weekday()],
                    start_time='15:40', end_time='16:10', start_date=self.day.isoformat())
        result = create_block.execute(self.user, **args)
        self.assertFalse(result.success)
        self.assertTrue(any(c['with_source']['type'] == 'scheduled_task' for c in result.data['conflicts']))
        args['start_date'] = (self.day+timedelta(days=7)).isoformat()
        self.assertTrue(create_block.execute(self.user, **args).success)

    def test_series_with_only_skipped_occurrence_does_not_block(self):
        RecurringBlockException.objects.create(user=self.user, recurring_block=self.course, date=self.day)
        result = create_block.execute(self.user, title='Autre', block_type='work', days=[self.day.weekday()],
            start_time='13:00', end_time='14:00', start_date=self.day.isoformat(), end_date=self.day.isoformat())
        self.assertTrue(result.success, result.message)

    def test_dated_override_replaces_template_time_for_bounded_conflict(self):
        RecurringBlockOverride.objects.create(user=self.user, recurring_block=self.course, date=self.day,
            start_time=time(18), end_time=time(19))
        args = dict(title='Autre', block_type='work', days=[self.day.weekday()],
                    start_date=self.day.isoformat(), end_date=self.day.isoformat())
        blocked = create_block.execute(self.user, **args, start_time='18:00', end_time='19:00')
        self.assertFalse(blocked.success)
        self.assertEqual(blocked.data['conflicts'][0]['scope'], 'dated_reservation')
        self.assertTrue(create_block.execute(self.user, **args, start_time='13:00', end_time='14:00').success)

    def test_recurring_night_bounds_consider_actual_adjacent_dates(self):
        monday = date(2030,12,2)
        night = RecurringBlock.objects.create(user=self.user, title='Nuit', block_type='work', day_of_week=6,
            start_time=time(23), end_time=time(1), flexibility='fixed', end_date=monday-timedelta(days=1))
        result = create_block.execute(self.user, title='Lundi', block_type='work', days=[0], start_time='00:30',
            end_time='02:00', start_date=monday.isoformat(), end_date=monday.isoformat())
        self.assertFalse(result.success, result.message)
        self.assertTrue(any(c['source']['id'] == night.pk for c in result.data['conflicts']))

    def test_restore_cannot_hide_conflict_with_a_task_created_after_skip(self):
        exception = RecurringBlockException.objects.create(user=self.user, recurring_block=self.course, date=self.day)
        self.assertTrue(schedule_task_at.execute(self.user, title='Remplacement', date=self.day.isoformat(),
            start_time='13:00', end_time='14:00').success)
        result = restore_block_occurrence.execute(self.user, date=self.day.isoformat(), title='Cours')
        self.assertFalse(result.success)
        self.assertEqual(result.cause, 'planning_conflict')
        self.assertTrue(RecurringBlockException.objects.filter(pk=exception.pk).exists())

    def test_requested_task_travel_cannot_overlap_an_existing_activity(self):
        task = Task.objects.create(user=self.user, title='Visite',
            place=UserPlace.objects.create(user=self.user, name='Clinique', travel_minutes=30))
        result = schedule_task_at.execute(self.user, title=task.title, date=self.day.isoformat(),
            start_time='16:30', end_time='17:00')
        self.assertFalse(result.success)
        self.assertTrue(any(c['requested_kind'] == 'transport' and c['source']['title'] == 'Révision'
                            for c in result.data['conflicts']))
        self.assertFalse(ScheduledBlock.objects.filter(task=task).exists())

    def test_legacy_automatic_scheduler_uses_effective_dated_constraints(self):
        from services.ai_scheduler import AIScheduler
        RecurringBlockException.objects.create(user=self.user, recurring_block=self.course, date=self.day)
        actual = AIScheduler()._get_blocked_times(self.user, self.day)
        self.assertEqual(actual, [(time(15,20), time(16,20))])

    def test_sleep_source_is_preserved_instead_of_absorbing_night_work(self):
        day = self.day + timedelta(days=2)
        RecurringBlock.objects.create(user=self.user, title='Quart', block_type='work', day_of_week=day.weekday(),
            start_time=time(19), end_time=time(2), flexibility='fixed')
        sleep = RecurringBlock.objects.create(user=self.user, title='Sommeil', block_type='sleep',
            day_of_week=day.weekday(), start_time=time(23), end_time=time(7))
        result = schedule_task_at.execute(self.user, title='Appel', date=(day+timedelta(days=1)).isoformat(),
                                          start_time='09:00', end_time='09:30')
        self.assertFalse(result.success)
        self.assertEqual(result.data['conflicts'][0]['source']['id'], sleep.pk)
        self.assertEqual(result.data['conflicts'][0]['policy'], 'recovery_sleep')
        self.assertEqual(result.data['conflicts'][0]['start_time'], '02:20')

    def test_optimization_counts_dated_tasks_and_detector_ignores_movable_sport(self):
        from services.agent.tools import suggest_schedule_optimization
        self.course.delete()
        RecurringBlock.objects.create(user=self.user, title='Sport', block_type='sport', day_of_week=self.day.weekday(),
            start_time=time(15,30), end_time=time(16))
        self.assertEqual(detect_conflicts.execute(self.user, start_date=self.day.isoformat()).data['count'], 0)
        with patch('services.agent.tools.suggest_schedule_optimization.timezone.localdate', return_value=self.day):
            data = suggest_schedule_optimization.execute(self.user).data
        self.assertGreaterEqual(data['analysis']['daily_hours']['Jeudi'], 1)
        self.assertIn('task', data['analysis']['block_types'])

    def test_alternative_windows_account_for_new_reservations_own_margins(self):
        result = find_free_slots.execute(self.user, date=self.day.isoformat(), reservation_type='recurring')
        self.assertEqual(result.data['free_slots'][0]['end_time'], '12:20')
        self.assertEqual(result.data['free_slots'][-1]['start_time'], '16:40')
        self.assertEqual(result.data['reservation_margins'], {'before_minutes': 20, 'after_minutes': 20})

    def test_split_overnight_task_has_no_invented_midnight_trip(self):
        day = self.day + timedelta(days=1)
        task = Task.objects.create(user=self.user, title='Nuit',
            place=UserPlace.objects.create(user=self.user, name='Usine', travel_minutes=30))
        result = schedule_task_at.execute(self.user, title=task.title, date=day.isoformat(),
                                          start_time='22:00', end_time='02:00')
        self.assertTrue(result.success, result.message)
        parts = fixed_constraints(self.user, day+timedelta(days=1))
        self.assertEqual([(c.start,c.end,c.kind) for c in parts], [(0,120,'activity'), (120,150,'transport')])

    def test_runtime_persists_conflict_provenance_and_references_large_reads(self):
        from django.utils import timezone
        from core.models import AgentRun, Conversation, AgentOperation
        from services.agent_runtime.tools import execute_call
        from services.agent_runtime.engine import compact_result, _read_result
        self.user.profile.ai_consent_at = timezone.now()
        self.user.profile.save()
        conv = Conversation.objects.create(user=self.user, engine_version='v3')
        run = AgentRun.objects.create(user=self.user, conversation=conv, payload_hash='test', status='running')
        result = execute_call(run.pk, 'blocked', 'schedule_task_at',
            dict(title='Sortie', date=self.day.isoformat(), start_time='12:30', end_time='13:00'))
        self.assertFalse(result['success'])
        persisted = AgentOperation.objects.get(pk=result['operation_id']).result
        self.assertEqual(persisted['data']['conflicts'][0]['source']['id'], self.course.pk)
        self.assertFalse(Task.objects.filter(user=self.user, title='Sortie').exists())
        for i in range(45):
            RecurringBlock.objects.create(user=self.user, title=f'Cours {i}', block_type='course',
                day_of_week=self.day.weekday(), start_time=time(8), end_time=time(9), flexibility='fixed')
        read = execute_call(run.pk, 'read', 'get_today_schedule', {'date': self.day.isoformat()})
        self.assertTrue(compact_result(read)['data']['truncated'])
        page = _read_result(run.pk, read['operation_id'], ['constraints'], 0, 5)
        self.assertEqual(len(page['data']),5)
        self.assertTrue(all(c['source']['title'] and c['kind'] for c in page['data']))
        self.assertTrue(page['truncated'])


class DecisionProtocolTests(SimpleTestCase):
    def test_decision_survives_reads_and_only_answer_or_new_request_releases_it(self):
        from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
        from services.agent_runtime.decision import decision_pending
        history = [ModelRequest(parts=[ToolReturnPart('schedule_task_at',
            {'data': {'requires_user_decision': True}}, 'conflict')])]
        self.assertTrue(decision_pending(history))
        history.append(ModelRequest(parts=[ToolReturnPart('find_free_slots', {'success':True}, 'read')]))
        self.assertTrue(decision_pending(history))
        answered = ModelRequest(parts=[ToolReturnPart('ask_question_custom',
            {'success':True,'data':{'answer':{'text':'demain'}}}, 'question')])
        self.assertFalse(decision_pending(history+[answered]))
        self.assertFalse(decision_pending(history+[ModelRequest(parts=[UserPromptPart('Abandonne cette demande.')])]))

    def test_prose_and_ordinary_tool_errors_cannot_force_a_question(self):
        from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
        from services.agent_runtime.decision import decision_pending
        self.assertFalse(decision_pending([ModelRequest(parts=[UserPromptPart('Et toi comment tu vas ?')])]))
        self.assertFalse(decision_pending([ModelRequest(parts=[ToolReturnPart('read',
            {'success':False,'message':'requires_user_decision'}, 'x')])]))

    def test_required_choice_keeps_reads_and_disables_writes_only_until_answered(self):
        from pydantic_ai.messages import ModelRequest, ToolReturnPart
        from pydantic_ai.models import ModelRequestParameters
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.tools import ToolDefinition
        from services.agent_runtime.decision import DecisionModel
        model = DecisionModel(FunctionModel(lambda messages,info: None))
        parameters = ModelRequestParameters(function_tools=[ToolDefinition(name=n) for n in
            ('create_block','find_free_slots','ask_question_custom')], allow_text_output=True)
        history = [ModelRequest(parts=[ToolReturnPart('create_block',
            {'data':{'requires_user_decision':True}}, 'x')])]
        _, actual = model.parameters(history, {}, parameters)
        self.assertFalse(actual.allow_text_output)
        self.assertEqual([t.name for t in actual.function_tools], ['find_free_slots','ask_question_custom'])
        self.assertTrue(parameters.allow_text_output)
        self.assertIs(model.parameters([], {}, parameters)[1], parameters)

    def test_large_result_keeps_decision_contract_outside_paginated_data(self):
        from services.agent_runtime.engine import compact_result
        result = compact_result({'operation_id':'x','success':False,'data':{
            'requires_user_decision':True,'large':'x'*20000}})
        self.assertTrue(result['requires_user_decision'])
        self.assertTrue(result['data']['truncated'])
