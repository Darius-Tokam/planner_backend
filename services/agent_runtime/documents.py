"""Extract first. Only explicit tools may change the planning."""
from copy import deepcopy
from dbos import DBOS
from django.db import transaction

from core.models import AgentRun, UploadedDocument
from core.ai_consent import has_ai_consent
from services.document_processor import DocumentProcessor, DocumentParseError
from . import store


class ExtractionProcessor(DocumentProcessor):
    def __init__(self, run_id):
        super().__init__()
        self.run_id = run_id

    @transaction.atomic
    def _materialize_document(self, document, data):
        run = AgentRun.objects.select_for_update().select_related('user').get(pk=self.run_id)
        type(run.user).objects.select_for_update().get(pk=run.user_id)
        if run.status == 'cancelled' or not has_ai_consent(run.user):
            raise PermissionError('Extraction authorization revoked')
        current = UploadedDocument.objects.select_for_update().get(pk=document.pk, user=run.user)
        if current.processed:
            return current.extracted_data
        if not isinstance(data, dict) or data.get('parse_error'):
            raise DocumentParseError('Extraction invalide')
        self._verifier_echeance()
        current.extracted_data = {k: deepcopy(v) for k, v in data.items() if not k.startswith('_')}
        current.extracted_data['_runtime_extraction_only'] = True
        current.processed, current.processing_error = True, None
        current.save(update_fields=['extracted_data', 'processed', 'processing_error'])
        document.refresh_from_db()
        return current.extracted_data


@DBOS.step(name='planner.v3.document', retries_allowed=False)
def process_document(run_id, document_id):
    from services.agent.tools.process_uploaded_document import execute
    from core.views import _chat_doc_data
    run = AgentRun.objects.select_related('user').get(pk=run_id)
    if run.status == 'cancelled' or not has_ai_consent(run.user):
        return {'success': False, 'cause': 'run_stopped', 'document_id': document_id}
    document = UploadedDocument.objects.get(pk=document_id, user=run.user)
    store.emit(run_id, 'document', {'id': document.pk, 'status': 'processing'}, key=f'document:{document.pk}:processing')
    try:
        if not document.processed:
            ExtractionProcessor(run_id).process_document(document)
    except Exception:
        document.refresh_from_db()
        data = _chat_doc_data(document)
        store.emit(run_id, 'document', data, key=f'document:{document.pk}:failed')
        return {'success': False, 'cause': 'extraction_failed', 'document_id': document_id,
                'message': 'L’extraction a échoué ou son autorisation a été révoquée. Aucune modification du planning.'}
    document.refresh_from_db()
    data = _chat_doc_data(document)
    store.emit(run_id, 'document', data, key=f'document:{document.pk}:ready')
    return store.commit_operation(run_id, f'extraction:{document_id}', 'process_uploaded_document',
        {'document_id': document_id}, lambda: execute(run.user, document_id=document_id).to_dict())
