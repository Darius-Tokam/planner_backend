"""Single PydanticAI loop. Custom tools have explicit DBOS steps."""
import json
import time
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from dbos import DBOS
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import PartStartEvent, PartDeltaEvent, TextPart, TextPartDelta, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import Tool

from core.models import AgentOperation
from services.agent.tools import TOOL_MAP
from . import policy, store
from .prompts import SYSTEM
from .tools import execute_call, schema_for, failure

CORE_TOOLS = frozenset({
    'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task',
    'list_blocks', 'create_block', 'update_block', 'delete_block', 'schedule_task_at',
    'get_today_schedule', 'get_week_schedule', 'find_free_slots',
    'list_goals', 'create_goal', 'update_goal', 'manage_reminder', 'list_reminders',
    'send_notification', 'read_planning_skill', 'ask_question_custom',
})


@dataclass
class Dependencies:
    run_id: str
    invocation_id: str
    durable: bool = False
    enabled: set = field(default_factory=lambda: set(CORE_TOOLS))
    request_key: str = ''
    attempt_id: str = ''


def compact_result(result):
    if len(json.dumps(result, default=str)) <= 16000 or not result.get('operation_id'):
        return result
    return {key: value for key, value in result.items() if key != 'data'} | {
        'data': {'truncated': True, 'result_ref': result['operation_id'],
                 'available_fields': list(result.get('data', {})),
                 'instruction': 'Lire les champs nécessaires via read_tool_result.'}}


@DBOS.step(name='planner.v3.tool', retries_allowed=False)
def durable_tool(run_id, call_id, name, arguments):
    return execute_call(run_id, call_id, name, arguments)


def _read_result(run_id, operation_id, path, offset, limit):
    # Scope to this conversation's owner; no arbitrary database object read.
    from core.models import AgentRun
    run = AgentRun.objects.get(pk=run_id)
    operation = AgentOperation.objects.filter(pk=operation_id, run__user_id=run.user_id,
                                               run__conversation_id=run.conversation_id).first()
    if operation is None:
        return failure('result_not_found', 'Résultat absent de cette conversation.')
    value = operation.result.get('data', {})
    try:
        for key in path:
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (TypeError, ValueError, IndexError, KeyError):
        return failure('invalid_path', 'Chemin absent du résultat.')
    if isinstance(value, (list, str)):
        page = value[offset:offset + limit]
        return {'data': page, 'total': len(value), 'offset': offset, 'truncated': offset + limit < len(value)}
    return {'data': value, 'truncated': False}


@DBOS.step(name='planner.v3.read_result', retries_allowed=False)
def durable_read_result(*args):
    return _read_result(*args)


async def stream_events(ctx, stream):
    deps = ctx.deps
    prefix = deps.request_key or f'{deps.invocation_id}:{ctx.run_step}'
    texts, published, last = {}, {}, 0.0

    async def publish(index, force=False):
        nonlocal last
        text = texts[index]
        old = published.get(index)
        now = time.monotonic()
        if text == old or (not force and old is not None and len(text) - len(old) < 80 and now - last < .1):
            return
        await sync_to_async(store.emit)(deps.run_id, 'text', {
            'part_id': f'{prefix}:{index}', 'attempt_id': deps.attempt_id,
            'text': text}, key=f'text:{prefix}:{deps.attempt_id}:{index}:{len(text)}')
        published[index], last = text, now

    async for event in stream:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            texts[event.index] = event.part.content
            await publish(event.index, force=True)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            texts[event.index] = texts.get(event.index, '') + event.delta.content_delta
            await publish(event.index)
    for index in texts:
        await publish(index, force=True)


def build_agent(model, *, durable=False):
    tools = []
    for name, module in TOOL_MAP.items():
        if name in policy.EXCLUDED:
            continue

        def make_tool(tool_name):
            async def invoke(ctx, **arguments):
                # Tool-call order is not authorization. A clarification in this
                # response postpones every mutation, including calls before it.
                if tool_name in policy.MUTATIONS or tool_name == 'send_notification':
                    if ctx.messages and any(isinstance(part, ToolCallPart) and part.tool_name == 'ask_question_custom'
                                            for part in ctx.messages[-1].parts):
                        return failure('clarification_pending', 'Attends la réponse à la question avant toute modification. Rappelle ensuite les outils nécessaires.')
                fn = durable_tool if ctx.deps.durable else execute_call
                result = await sync_to_async(fn)(ctx.deps.run_id, ctx.tool_call_id, tool_name, arguments)
                if result.get('status') == 'waiting':
                    raise CallDeferred(metadata={'interaction_id': result['interaction_id']})
                return compact_result(result)
            return invoke

        async def prepare(ctx, definition):
            # Rebuild discovery from the persisted protocol too: a DBOS replay
            # may return a cached load_tools result without executing its body.
            enabled = set(ctx.deps.enabled)
            for message in ctx.messages:
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == 'load_tools' and isinstance(part.content, dict):
                        enabled.update(part.content.get('available_next_request', []))
            return definition if definition.name in enabled else None

        description = module.DESCRIPTION
        for legacy_question in policy.EXCLUDED:
            description = description.replace(legacy_question, 'ask_question_custom')
        if name == 'delete_task':
            description = ('Prépare la suppression définitive d’une tâche identifiée par list_tasks, avec ses créneaux. '
                           'Appelle cet outil directement pour afficher la confirmation sécurisée; '
                           'le serveur attend la décision humaine avant toute suppression. Pour cocher, utilise complete_task.')
        elif name == 'clear_all_blocks':
            description = ('Prépare l’archivage de tous les blocs récurrents si demandé explicitement. '
                           'Appelle cet outil pour afficher la confirmation; le serveur attend la décision avant d’archiver.')
        elif name == 'create_block':
            description = ('Crée des créneaux hebdomadaires uniquement quand la répétition est demandée. '
                           'Un jour ou une date unique utilise schedule_task_at. Nécessite titre, jours, début et fin. '
                           'Ne déduis jamais la répétition du type d’activité. Une fin avant le début traverse minuit. '
                           'Les conflits sont retournés par l’outil.')
        elif name == 'schedule_task_at':
            description = ('Réserve un événement unique : titre, date, heure de début et de fin. '
                           'Le nom générique donné par l’utilisateur suffit comme titre. '
                           'Une fin avant le début traverse minuit. Une nouvelle invocation avec le même titre '
                           'et la même date met à jour ce créneau. Les autres champs sont facultatifs. '
                           'Si une heure imposée manque, demande uniquement cette heure avec ask_question_custom.')
        if name == 'ask_question_custom':
            description = ('Suspend l’action pour obtenir UNIQUEMENT les données obligatoires qui manquent encore. '
                           'Reprends toutes les valeurs déjà connues, même un titre générique. '
                           'Aucune question facultative, proposition supplémentaire ou reconfirmation d’une valeur explicite. '
                           'text: libre; choice: 2 à 4 choix exhaustifs; form: seulement les champs indispensables manquants, '
                           'ou radio avec allow_other. Une réponse '
                           'reprend exactement cet appel. Les confirmations sont créées par l’outil métier.')
        tool = Tool.from_schema(make_tool(name), name, description, schema_for(name), takes_ctx=True,
                                sequential=name in policy.MUTATIONS or name == 'send_notification')
        tool.prepare = prepare
        tools.append(tool)

    async def load_tools(ctx, names: list[str]):
        allowed = set(TOOL_MAP) - policy.EXCLUDED
        if len(names) > 12 or any(name not in allowed for name in names):
            return failure('invalid_tool_names', 'Choisis des noms dans le catalogue.', {'names': sorted(allowed)})
        ctx.deps.enabled.update(names)
        return {'available_next_request': names}

    async def read_tool_result(ctx, operation_id: str, path: list[str] = [], offset: int = 0, limit: int = 20):
        if not 0 <= offset or not 1 <= limit <= 4000 or len(path) > 8:
            return failure('invalid_page', 'Vérifie le chemin, le début et la taille de la portion.')
        fn = durable_read_result if ctx.deps.durable else _read_result
        return await sync_to_async(fn)(ctx.deps.run_id, operation_id, path, offset, limit)

    catalog = '\n'.join(f'{name}: {module.DESCRIPTION[:180]}' for name, module in TOOL_MAP.items()
                        if name not in CORE_TOOLS and name not in policy.EXCLUDED)
    tools.extend([Tool(load_tools, takes_ctx=True, description='Active les schémas complets du catalogue:\n' + catalog),
                  Tool(read_tool_result, takes_ctx=True, description='Relit une portion d’un résultat volumineux par référence.')])
    agent = Agent(model, name='planner_v3', deps_type=Dependencies, output_type=[str, DeferredToolRequests],
                  instructions=SYSTEM, tools=tools, event_stream_handler=stream_events, retries=2)
    if durable:
        from pydantic_ai.durable_exec.dbos import DBOSAgent
        return DBOSAgent(agent, model_step_config={'retries_allowed': False})
    return agent
