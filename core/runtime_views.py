import json
import time
import asyncio
from asgiref.sync import sync_to_async

from django.http import StreamingHttpResponse
from django.db import close_old_connections, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

from core.ai_consent import ai_consent_denied
from core.models import AgentInteraction, AgentRun, Conversation
from core.chat_turns import _fingerprint
from services.agent_runtime import admission, store


def usage_error(user):
    from services.agent_v2.budget_tour import statut_usage
    state = statut_usage(user)
    if state['bloque_par']:
        cause = state['bloque_par']
        message = ('Ton quota quotidien de jetons mesurés est atteint.' if cause == 'quota_mesure'
                   else 'La protection de coût a suspendu les appels. Certaines mesures sont indisponibles.')
        return Response({'code': cause, 'error': message, 'usage': state,
                         'signup_suggested': bool(getattr(user.profile, 'is_anonymous', False))}, status=429)
    return None


class RuntimeConfigView(APIView):
    def get(self, request):
        return Response({'default_engine': admission.default_engine(request.user), 'supported': ['v2', 'v3']})


class RuntimeRunListView(APIView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    throttle_classes = [UserRateThrottle, AnonRateThrottle]

    def get_throttles(self):
        from core.views import AnonymousChatThrottle
        return [*super().get_throttles(), AnonymousChatThrottle()]

    def post(self, request):
        denied = ai_consent_denied(request.user)
        if denied is not None:
            return denied
        from core.views import _validate_chat_files
        files = request.FILES.getlist('attachment')
        invalid = _validate_chat_files(files)
        if invalid is not None:
            return invalid
        message = request.data.get('message', '')
        if not isinstance(message, str) or len(message) > 20000 or not (message.strip() or files):
            return Response({'error': 'Écris un message de moins de 20 000 caractères ou joins un fichier.'}, status=400)
        try:
            # Replays do not consume quota and remain readable after a quota stop.
            existing = AgentRun.objects.filter(pk=request.data.get('turn_id'), user=request.user).first()
            if existing is None:
                limited = usage_error(request.user)
                if limited is not None:
                    return limited
            run = admission.admit(request.user, request.data.get('conversation_id'), request.data.get('turn_id'),
                message, _fingerprint(request), files, request.data.get('document_type', 'other'))
        except Conversation.DoesNotExist:
            return Response({'error': 'Conversation introuvable.'}, status=404)
        except store.Conflict as exc:
            return Response({'code': 'run_conflict', 'error': str(exc)}, status=409)
        except (ValueError, TypeError, KeyError, ValidationError):
            return Response({'error': 'Identifiant ou demande invalide.'}, status=400)
        from core.views import _chat_doc_data
        message_row = run.conversation.messages.filter(role='user', metadata__run_id=str(run.pk)).first()
        payload = admission.snapshot(run)
        payload['attachments'] = [_chat_doc_data(doc) for doc in message_row.attachments.all()] if message_row else []
        return Response(payload, status=202)


class RuntimeRunView(APIView):
    def get(self, request, run_id):
        try:
            run = AgentRun.objects.get(pk=run_id, user=request.user)
            after = max(0, int(request.query_params.get('after', 0)))
        except AgentRun.DoesNotExist:
            return Response({'error': 'Demande introuvable.'}, status=404)
        except ValueError:
            return Response({'error': 'Curseur invalide.'}, status=400)
        return Response(admission.snapshot(run, after))

    def delete(self, request, run_id):
        with transaction.atomic():
            run = AgentRun.objects.select_for_update().filter(pk=run_id, user=request.user).first()
            if run is None:
                return Response({'error': 'Demande introuvable.'}, status=404)
            if run.status not in ('completed', 'cancelled'):
                run.status = 'cancelled'
                run.save(update_fields=['status', 'updated_at'])
                run.interactions.filter(status='pending').update(status='cancelled')
                store.emit(run.pk, 'status', {'status': 'cancelled',
                    'message': 'Demande arrêtée. Les actions déjà vérifiées restent enregistrées.'}, key='cancelled')
        return Response(admission.snapshot(run))


class RuntimeAnswerView(APIView):
    def post(self, request, interaction_id):
        denied = ai_consent_denied(request.user)
        if denied is not None:
            return denied
        try:
            run = admission.resume(request.user, interaction_id, request.data.get('answer'))
        except AgentInteraction.DoesNotExist:
            return Response({'error': 'Question introuvable.'}, status=404)
        except store.InvalidAnswer as exc:
            return Response({'error': str(exc)}, status=400)
        except store.Conflict as exc:
            return Response({'error': str(exc)}, status=409)
        return Response(admission.snapshot(run), status=202)


def event_stream(user_id, run_id, after, *, max_seconds=25):
    end = time.monotonic() + max_seconds
    cursor, last_heartbeat = after, time.monotonic()
    while time.monotonic() < end:
        close_old_connections()
        run = AgentRun.objects.filter(pk=run_id, user_id=user_id).first()
        if run is None:
            return
        events = list(run.events.filter(seq__gt=cursor)[:100])
        for event in events:
            cursor = event.seq
            body = json.dumps({'seq': event.seq, 'type': event.kind, 'data': event.data}, ensure_ascii=False)
            yield f'id: {cursor}\nevent: runtime\ndata: {body}\n\n'
        if run.status not in ('queued', 'running') and cursor >= run.next_seq:
            return
        if not events:
            if time.monotonic() - last_heartbeat >= 5:
                yield ': heartbeat\n\n'
                last_heartbeat = time.monotonic()
            time.sleep(.25)
    yield ': reconnect\n\n'


class RuntimeStreamView(APIView):
    def get(self, request, run_id):
        if not AgentRun.objects.filter(pk=run_id, user=request.user).exists():
            return Response({'error': 'Demande introuvable.'}, status=404)
        try:
            after = max(0, int(request.query_params.get('after', request.headers.get('Last-Event-ID', 0))))
        except ValueError:
            return Response({'error': 'Curseur invalide.'}, status=400)
        reader = async_event_stream if hasattr(request._request, 'scope') else event_stream
        response = StreamingHttpResponse(reader(request.user.pk, run_id, after), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache, no-transform'
        response['X-Accel-Buffering'] = 'no'
        return response


async def async_event_stream(user_id, run_id, after, *, max_seconds=25):
    # An async iterator is essential under ASGI: Django buffers sync iterators.
    end, cursor = time.monotonic() + max_seconds, after
    def batch():
        close_old_connections()
        run = AgentRun.objects.filter(pk=run_id, user_id=user_id).first()
        return admission.snapshot(run, cursor) if run else None
    while time.monotonic() < end:
        state = await sync_to_async(batch)()
        if state is None:
            return
        for event in state['events']:
            cursor = event['seq']
            yield f'id: {cursor}\nevent: runtime\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
        if state['status'] not in ('queued', 'running') and not state['has_more']:
            return
        if not state['events']:
            yield ': heartbeat\n\n'
            await asyncio.sleep(.5)
    yield ': reconnect\n\n'
