"""Provider boundary: one ledger entry per real call, cached result on replay."""
import json
import time
from contextlib import asynccontextmanager

import anyio
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.durable_exec.dbos._model import DBOSStreamedResponse

from core.models import AgentModelAttempt
from services.agent_v2.budget_tour import (
    AppelFournisseur, ReservationTour, ouvrir_trace, fermer_trace, _models,
)
from services.agent_v2.trace_tour import trace_courante


def start_attempt(invocation_id, key, phase, provider, input_bound, max_output):
    from core.models import AgentInvocation
    invocation = AgentInvocation.objects.select_related('run').get(pk=invocation_id)
    # A killed request has unknown usage. Keep its cost provision, release only
    # unused reservation capacity. A replayed successful response costs nothing.
    with transaction.atomic():
        for stale in AgentModelAttempt.objects.select_for_update().filter(
                invocation=invocation, request_key=key, status='started'):
            Budget, Reservation = _models()
            if stale.reservation_id:
                reservation = Reservation.objects.get(pk=stale.reservation_id)
                ReservationTour(reservation, 0, 0).fermer()
                from django.db.models import F
                Budget.objects.filter(pk=reservation.budget_id).update(appels_non_mesures=F('appels_non_mesures') + 1)
            stale.status = 'unknown'
            stale.save(update_fields=['status'])
    user = get_user_model().objects.get(pk=invocation.run.user_id)
    from core.ai_consent import has_ai_consent
    if not has_ai_consent(user):
        raise PermissionError('AI consent required')
    if invocation.run.status == 'cancelled':
        raise InterruptedError('Run cancelled')
    trace = ouvrir_trace(user, delai=120)
    token = trace_courante.set(trace)
    try:
        call = AppelFournisseur(phase, provider, input_bound, max_output)
        attempt = AgentModelAttempt.objects.create(invocation=invocation, request_key=key,
                                                   reservation_id=trace.reservation.pk)
        return attempt, trace, call
    except Exception:
        fermer_trace(trace)
        raise
    finally:
        trace_courante.reset(token)


def finish_attempt(attempt_id, trace, call, response, elapsed_ms):
    try:
        with transaction.atomic():
            attempt = AgentModelAttempt.objects.select_for_update().get(pk=attempt_id)
            if attempt.status != 'started':
                return
            usage = response.usage if response is not None else None
            measured = ({'entree': usage.input_tokens, 'sortie': usage.output_tokens,
                         'total': usage.total_tokens, 'cache': usage.cache_read_tokens} if usage else {})
            call.terminer(measured, response is not None)
            attempt.usage = {**measured, 'usage_connu': bool(measured.get('total', 0) > 0)}
            attempt.status = 'completed' if response is not None else 'unknown'
            attempt.response = json.loads(ModelMessagesTypeAdapter.dump_json([response])) if response else []
            attempt.elapsed_ms, attempt.finished_at = elapsed_ms, timezone.now()
            attempt.save()
    finally:
        fermer_trace(trace)


class AccountedModel(WrapperModel):
    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        if run_context is None:
            raise RuntimeError('A durable run context is required.')
        deps = run_context.deps
        key = f'{deps.invocation_id}:{run_context.run_step}'
        deps.request_key = key
        cached = await sync_to_async(lambda: AgentModelAttempt.objects.filter(
            invocation_id=deps.invocation_id, request_key=key, status='completed').first())()
        if cached:
            deps.attempt_id = str(cached.pk)
            response = ModelMessagesTypeAdapter.validate_python(cached.response)[0]
            yield DBOSStreamedResponse(model_request_parameters, response)
            return
        size = len(ModelMessagesTypeAdapter.dump_json(messages)) + len(TypeAdapter(ModelRequestParameters).dump_json(model_request_parameters))
        settings = dict(model_settings or {})
        attempt, trace, call = await sync_to_async(start_attempt)(deps.invocation_id, key,
            'planning', self.system, (size + 2) // 3 + 512, settings.get('max_tokens', 2048))
        deps.attempt_id = str(attempt.pk)
        settings['max_tokens'] = call.max_sortie
        settings.setdefault('timeout', 30)
        response, start = None, time.monotonic()
        try:
            with anyio.fail_after(min(call.delai, 90)):
                async with self.wrapped.request_stream(messages, settings, model_request_parameters, run_context) as stream:
                    yield stream
                    response = stream.get()
        finally:
            await sync_to_async(finish_attempt)(attempt.pk, trace, call, response,
                                               int((time.monotonic() - start) * 1000))

    async def request(self, messages, model_settings, model_request_parameters):
        raise RuntimeError('Use agent.run with its event stream handler and run context.')
