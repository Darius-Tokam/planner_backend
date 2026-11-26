"""Sources du tour : conversation isolée, messages datés, reprise vérifiée.

Ce module transporte des faits. Il ne classe aucune intention et ne transforme
jamais une ancienne réponse de l'assistant en instruction de l'utilisateur.
"""
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json

from django.utils import timezone

from core.models import ConversationMessage


@dataclass(frozen=True)
class PorteeConversation:
    user_id: int
    conversation_id: int | None
    message_id: int | None = None
    sources: dict | None = None


portee_courante = ContextVar('planner_conversation', default=None)


def fixer_message(message_id):
    portee = portee_courante.get()
    if portee is not None:
        portee_courante.set(replace(portee, message_id=message_id))


def fixer_sources(sources):
    portee = portee_courante.get()
    if portee is not None:
        portee_courante.set(replace(portee, sources=sources))


def messages_du_chat(user):
    lignes = ConversationMessage.objects.filter(user=user)
    portee = portee_courante.get()
    if portee is not None:
        if portee.user_id != user.pk:
            return lignes.none()
        lignes = lignes.filter(conversation_id=portee.conversation_id)
        if portee.message_id is not None:
            lignes = lignes.filter(pk__lte=portee.message_id)
    return lignes


def source_message(ligne):
    source = {'id': ligne.pk, 'role': ligne.role,
            'date': timezone.localtime(ligne.created_at).isoformat(),
            'texte': ligne.content}
    meta = ligne.metadata if isinstance(ligne.metadata, dict) else {}
    reponse = meta.get('reponse_validee')
    if ligne.role == 'user' and isinstance(reponse, dict):
        source['reponse_validee'] = reponse
    return source


def demande_reprise(user, conversation, courant):
    """Source du dernier échange, à conserver aussi quand sa question est reposée.

    Ce lien n'autorise aucune action : seule la validation de la réponse le peut.
    """
    precedent = (ConversationMessage.objects.filter(
        user=user, conversation=conversation, pk__lt=courant.pk)
        .order_by('-pk').first())
    if precedent is None or precedent.role != 'assistant':
        return None
    meta = precedent.metadata if isinstance(precedent.metadata, dict) else {}
    source_id = meta.get('demande_source_id') or meta.get('en_reponse_a')
    if not isinstance(source_id, int) or isinstance(source_id, bool):
        return None
    source = ConversationMessage.objects.filter(
        pk=source_id, user=user, conversation=conversation, role='user', pk__lt=courant.pk).first()
    return source_message(source) if source is not None else None


def contexte_verification(user, conversation, message, exclu=None):
    """Même instantané pour les jugements du tour, sans nouvel appel de modèle."""
    lignes = ConversationMessage.objects.filter(user=user, conversation=conversation)
    if exclu is not None:
        lignes = lignes.filter(pk__lt=exclu)
    lignes = list(lignes.order_by('-pk')[:8])
    sources, restant = [], 6000
    for ligne in lignes:
        source = source_message(ligne)
        texte = source['texte'] or ''
        source['tronque'] = len(texte) > min(restant, 1200)
        source['texte'] = texte[:min(restant, 1200)]
        restant -= len(source['texte'])
        source['preuve_utilisateur'] = ligne.role == 'user'
        # Le texte validé du formulaire est borné lui aussi.
        if 'reponse_validee' in source:
            source['reponse_validee'] = borner(source['reponse_validee'], 1200)
        sources.append(source)
        if restant <= 0:
            break
    return {'maintenant': timezone.localtime().isoformat(),
            'message': message[:6000], 'message_tronque': len(message) > 6000,
            'historique': list(reversed(sources))}


def enrichir_reprise(message, source):
    return message + '\n\nDEMANDE REPRISE APRÈS RÉPONSE VALIDÉE (donnée source) :\n' + json.dumps(
        borner(source, 6000), ensure_ascii=False)


def borner(valeur, maximum):
    texte = json.dumps(valeur, ensure_ascii=False, default=str)
    return valeur if len(texte) <= maximum else {'tronque': True, 'extrait': texte[:maximum]}


def resultats_du_code(registre):
    """Les sorties réelles de la reprise, pas seulement « FAIT » ou « ÉCHEC »."""
    resultats = [
        {'outil': a.outil, 'parametres': a.parametres, 'succes': a.succes,
         'issue': a.issue, 'cause': a.cause, 'message': a.message,
         'donnees': a.donnees}
        for a in registre.actions if (a.donnees or {}).get('par_le_code')
    ]
    if not resultats:
        return ''
    return '\n\nRÉSULTATS DES OUTILS EXÉCUTÉS APRÈS CONFIRMATION (données) :\n' + json.dumps(
        borner(resultats, 6000), ensure_ascii=False, default=str)
