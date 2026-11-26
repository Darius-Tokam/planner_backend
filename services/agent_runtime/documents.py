"""Keep the existing document parser and its atomic materialization receipt."""
from dbos import DBOS

from core.models import AgentRun, UploadedDocument
from core.ai_consent import has_ai_consent
from services.agent_v2.registre import Registre
from services.agent_v2.reconciliation import reconcilier
from services.agent.tools.base import ToolResult
from . import store


@DBOS.step(name='planner.v3.document', retries_allowed=False)
def process_document(run_id, document_id):
    from services.document_processor import DocumentProcessor
    from services.agent_v2.importation import resume_import
    from core.views import _chat_doc_data
    run = AgentRun.objects.select_related('user').get(pk=run_id)
    if run.status == 'cancelled' or not has_ai_consent(run.user):
        return {'status': 'cancelled'}
    document = UploadedDocument.objects.get(pk=document_id, user=run.user)
    store.emit(run_id, 'document', {'id': document.pk, 'status': 'processing'}, key=f'document:{document.pk}:processing')
    try:
        if not document.processed:
            DocumentProcessor().process_document(document)
    except Exception:
        document.refresh_from_db()
        data = _chat_doc_data(document)
        store.emit(run_id, 'document', data, key=f'document:{document.pk}:failed')
        return data
    document.refresh_from_db()
    summary = resume_import(document)
    def receipt():
        result = ToolResult(True, data=summary, message='Document traité. Consulte les données et précisions restantes.')
        registry = Registre()
        action = registry.ajouter('import_document', {'document_id': document_id}, result)
        reconcilier(run.user, registry)
        return {**result.to_dict(), 'receipt': registry.par_id(action.id).recu}
    store.commit_operation(run_id, f'document:{document_id}', 'import_document', {'document_id': document_id}, receipt)
    data = _chat_doc_data(document)
    store.emit(run_id, 'document', data, key=f'document:{document.pk}:ready')
    return data
