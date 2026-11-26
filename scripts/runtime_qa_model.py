"""Controlled provider for browser transport QA, never semantic evaluation."""
import asyncio
import json
from datetime import timedelta
from django.utils import timezone
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.messages import ToolReturnPart, UserPromptPart


async def stream(messages, info):
    prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
    returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    # Exact fixture inputs are deliberate: this verifies transport, not intent.
    if prompts and prompts[-1] == 'Merci, bonne journée.':
        yield 'Bonne '
        await asyncio.sleep(.1)
        yield 'journée !'
    elif returns and returns[-1].tool_name == 'schedule_task_at':
        assert returns[-1].content['receipt']['statut'] == 'confirme'
        yield 'Ton examen est planifié de 15 h à 17 h.'
    elif returns and returns[-1].tool_name == 'ask_question_custom':
        answer = returns[-1].content['data']['answer']['fields']['hours']
        day = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()) % 7 or 7)
        yield {0: DeltaToolCall(name='schedule_task_at', tool_call_id='qa-schedule', json_args=json.dumps({
            'title': 'Examen QA', 'date': day.isoformat(), 'start_time': answer['start'], 'end_time': answer['end']}))}
    else:
        yield {0: DeltaToolCall(name='ask_question_custom', tool_call_id='qa-hours', json_args=json.dumps({
            'kind': 'form', 'question': 'À quelles heures commence et finit ton examen ?',
            'fields': [{'id': 'hours', 'type': 'time_range', 'label': 'Heures', 'required': True}]}))}


def model():
    return FunctionModel(stream_function=stream)
