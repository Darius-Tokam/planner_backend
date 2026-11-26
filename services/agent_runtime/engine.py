"""Single PydanticAI loop. Custom tools have explicit DBOS steps."""
import json
import time
from uuid import UUID
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from dbos import DBOS
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import PartStartEvent, PartDeltaEvent, TextPart, TextPartDelta, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import Tool

from core.models import AgentOperation
from .catalog import TOOL_MAP
from . import policy, store
from .prompts import SYSTEM
from .tools import execute_call, schema_for, failure
from .decision import DecisionModel, decision_pending

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
    if result.get('data', {}).get('requires_user_decision'):
        result = {**result, 'requires_user_decision': True}
    if len(json.dumps(result, default=str)) <= 16000 or not result.get('operation_id'):
        return result
    compact = {key: value for key, value in result.items() if key not in ('data', 'receipt', 'message')} | {
        'message': str(result.get('message', ''))[:1000],
        'data': {'truncated': True, 'result_ref': result['operation_id'],
                 'available_fields': list(result.get('data', {}))[:100],
                 'instruction': 'Lire les champs nécessaires via read_tool_result.'}}
    return compact if len(json.dumps(compact, default=str)) <= 16000 else {
        'operation_id': result['operation_id'], 'success': result.get('success'),
        'data': {'truncated': True, 'result_ref': result['operation_id']}}


def bounded_page(value, offset, limit, path=()):
    if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 4000:
        return failure('invalid_page', 'Portion invalide.')
    if isinstance(value, dict):
        keys = list(value)[offset:offset + min(limit, 100)]
        page = {key: value[key] for key in keys}
    elif isinstance(value, (list, str)):
        page = value[offset:offset + limit]
    else:
        return {'data': value, 'truncated': False}
    total = len(value)
    truncated = offset + len(page) < total
    # Large individual fields are references, not silently chopped facts.
    def entry(item, child_path):
        nonlocal truncated
        if len(json.dumps(item, ensure_ascii=True)) > 1000:
            truncated = True
            return {'read_path': child_path, 'truncated': True, 'type': type(item).__name__}
        return item
    if isinstance(page, dict):
        page = {key: entry(item, [*path, key]) for key, item in page.items()}
    elif isinstance(page, list):
        page = [entry(item, [*path, str(offset + i)]) for i, item in enumerate(page)]
    result = {'data': page, 'total': total, 'offset': offset, 'truncated': truncated}
    while len(json.dumps(result)) > 15000 and len(page) > 1:
        page = dict(list(page.items())[:len(page)//2]) if isinstance(page, dict) else page[:len(page)//2]
        result.update(data=page, truncated=True)
    result['next_offset'] = offset + len(page) if offset + len(page) < total else None
    return result


@DBOS.step(name='planner.v3.tool', retries_allowed=False)
def durable_tool(run_id, call_id, name, arguments):
    return execute_call(run_id, call_id, name, arguments)


def _read_result(run_id, operation_id, path, offset, limit):
    # Scope to this conversation's owner; no arbitrary database object read.
    from core.models import AgentRun
    run = AgentRun.objects.get(pk=run_id)
    try:
        operation_id = UUID(str(operation_id))
    except (ValueError, TypeError):
        return failure('invalid_reference', 'Référence de résultat invalide.')
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
    return bounded_page(value, offset, limit, path)


def _read_turn(run_id, previous_id, field, offset, limit):
    from core.models import AgentRun
    try:
        previous_id = UUID(str(previous_id))
    except (ValueError, TypeError):
        return failure('invalid_reference', 'Référence de demande invalide.')
    run = AgentRun.objects.get(pk=run_id)
    previous = AgentRun.objects.filter(pk=previous_id, user_id=run.user_id,
        conversation_id=run.conversation_id).exclude(pk=run.pk).first()
    if previous is None or field not in ('prompt', 'answer', 'operations'):
        return failure('turn_not_found', 'Demande ou champ absent de cette conversation.')
    value = (previous.prompt if field == 'prompt' else previous.result.get('message', '') if field == 'answer'
             else [{'operation_id': str(op.pk), 'tool': op.tool} for op in previous.operations.order_by('created_at')])
    return bounded_page(value, offset, limit)


@DBOS.step(name='planner.v3.read_turn', retries_allowed=False)
def durable_read_turn(*args):
    return _read_turn(*args)


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
                    if decision_pending(ctx.messages):
                        return failure('decision_pending', 'Cette contrainte exige une décision humaine. Lis les disponibilités puis appelle ask_question_custom avant toute nouvelle modification.')
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
                           'Avec des heures explicites, appelle cet outil directement : il valide avant écriture et retourne les conflits.')
        elif name == 'schedule_task_at':
            description = ('Réserve un événement unique : titre, date, heure de début et de fin. '
                           'Le nom générique donné par l’utilisateur suffit comme titre. '
                           'Une fin avant le début traverse minuit. Une nouvelle invocation avec le même titre '
                           'et la même date met à jour ce créneau. Les autres champs sont facultatifs. '
                           'Avec des heures explicites, appelle cet outil directement : il valide avant écriture et retourne les conflits. '
                           'Si une heure imposée manque, demande uniquement cette heure avec ask_question_custom.')
        if name == 'ask_question_custom':
            description = ('Suspend l’action pour obtenir une information indispensable, résoudre une ambiguïté ou choisir une solution après un conflit. '
                           'Reprends toutes les valeurs déjà connues, même un titre générique. '
                           'Un horaire explicite en conflit exige un nouveau choix, jamais un déplacement silencieux. '
                           'Aucune question facultative ni reconfirmation d’une valeur explicite encore valide. '
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
    async def read_turn(ctx, run_id: str, field: str = 'prompt', offset: int = 0, limit: int = 1000):
        fn = durable_read_turn if ctx.deps.durable else _read_turn
        return await sync_to_async(fn)(ctx.deps.run_id, run_id, field, offset, limit)

    tools.extend([Tool(read_turn, takes_ctx=True, description='Relit prompt, answer ou operations d’une demande précédente par référence, sans perdre les corrections anciennes.'),
                  Tool(load_tools, takes_ctx=True, description='Active les schémas complets du catalogue:\n' + catalog),
                  Tool(read_tool_result, takes_ctx=True, description='Relit une portion d’un résultat volumineux par référence.')])
    agent = Agent(DecisionModel(model), name='planner_v3', deps_type=Dependencies, output_type=[str, DeferredToolRequests],
                  instructions=SYSTEM, tools=tools, event_stream_handler=stream_events, retries=2)
    @agent.output_validator
    async def require_resolved_decision(ctx, output):
        if isinstance(output, str) and decision_pending(ctx.messages):
            from pydantic_ai import ModelRetry
            raise ModelRetry('Une décision reste requise par un outil. Utilise ask_question_custom; tu peux encore lire les disponibilités avant de la formuler.')
        return output
    if durable:
        from pydantic_ai.durable_exec.dbos import DBOSAgent
        return DBOSAgent(agent, model_step_config={'retries_allowed': False})
    return agent
