"""A tool-requested human decision is protocol state, never prose detection."""
from contextlib import asynccontextmanager
from dataclasses import replace

from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.wrapper import WrapperModel


def decision_pending(messages):
    pending = False
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                pending = False
            elif isinstance(part, ToolReturnPart) and isinstance(part.content, dict):
                data = part.content.get('data') or {}
                if data.get('requires_user_decision') or part.content.get('requires_user_decision'):
                    pending = True
                elif part.tool_name == 'ask_question_custom' and part.content.get('success') and 'answer' in data:
                    pending = False
    return pending


class DecisionModel(WrapperModel):
    def parameters(self, messages, settings, parameters):
        if not decision_pending(messages):
            return settings, parameters
        # The model may still read to prepare useful options. No further writes
        # are offered until the answer resumes this exact question.
        from .policy import MUTATIONS
        available = [tool for tool in parameters.function_tools
                     if tool.name not in MUTATIONS and tool.name != 'send_notification']
        settings = dict(settings or {})
        if self.system == 'deepseek':
            # DeepSeek only supports required tool choice without thinking.
            settings['extra_body'] = {**settings.get('extra_body', {}), 'thinking': {'type': 'disabled'}}
            settings['extra_body'].pop('reasoning_effort', None)
        return settings, replace(parameters, allow_text_output=False, function_tools=available)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        settings, params = self.parameters(messages, model_settings, model_request_parameters)
        async with self.wrapped.request_stream(messages, settings, params, run_context) as stream:
            yield stream

    async def request(self, messages, model_settings, model_request_parameters):
        settings, params = self.parameters(messages, model_settings, model_request_parameters)
        return await self.wrapped.request(messages, settings, params)
