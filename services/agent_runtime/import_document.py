from copy import deepcopy
from pydantic import BaseModel, Field
from core.models import UploadedDocument
from services.agent.tools.base import ToolResult

NAME = 'import_document'
DESCRIPTION = ('Ajoute au planning les entrées extraites d’un document, uniquement si l’utilisateur demande cet import. '
               'Joindre un document pour lecture ou résumé ne demande aucune modification. '
               'Lis process_uploaded_document avant de choisir les actions nécessaires.')


class Parametres(BaseModel):
    document_id: int = Field(gt=0)


PARAMETERS = Parametres.model_json_schema()


def execute(user, **kwargs):
    from services.document_processor import DocumentProcessor
    from services.agent_v2.importation import resume_import
    doc = UploadedDocument.objects.select_for_update().get(user=user, pk=kwargs['document_id'])
    if not doc.processed or doc.processing_error:
        return ToolResult(False, data={'document_id': doc.pk}, message='L’extraction doit réussir avant l’import.')
    if doc.extracted_data.get('_runtime_extraction_only'):
        # The outer operation transaction keeps extraction intact on failure.
        data = {k: deepcopy(v) for k, v in doc.extracted_data.items() if not k.startswith('_')}
        doc.processed = False
        doc.save(update_fields=['processed'])
        DocumentProcessor()._persist_document(doc, data)
    return ToolResult(True, data=resume_import(doc), message='Import traité. Vérifie le reçu et les précisions restantes.')
