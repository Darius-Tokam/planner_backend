"""Continuité entre messages, indépendante des mots employés et des outils."""
import asyncio
import json
from datetime import date, datetime, time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.models import Conversation, ConversationMessage
from services.agent.tools.base import ToolResult
from services.agent_v2.agent import PlannerAgentV2
from services.agent_v2.registre import Registre
from services.agent_v2 import contexte_tour as contexte, jugement


class ContexteConversationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='continuite')
        self.chat = Conversation.objects.create(user=self.user)
        self.autre = Conversation.objects.create(user=self.user)
        self.agent = PlannerAgentV2()
        self.agent._conversation = self.chat

    def test_le_juge_ne_lit_pas_la_reponse_d_un_autre_chat(self):
        ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                           role='assistant', content='Quel jour pour cette action ?')
        ConversationMessage.objects.create(user=self.user, conversation=self.autre,
                                           role='assistant', content='Tout est terminé.')
        self.assertEqual(self.agent._dernier_mot_de_l_agent(self.user), 'Quel jour pour cette action ?')

    def test_historique_ancre_les_dates_relatives_dans_leur_message_source(self):
        source = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                    role='user', content='Prévois cela demain.')
        ConversationMessage.objects.filter(pk=source.pk).update(
            created_at=timezone.make_aware(datetime(2026, 11, 25, 21, 0)))
        historique = self.agent._historique(self.user)
        texte = historique[0].parts[0].content
        self.assertIn('2026-11-25', texte)
        self.assertIn('Prévois cela demain.', texte)

    def test_un_resultat_apres_confirmation_retourne_au_modele(self):
        for outil in ('delete_task', 'cancel_scheduled_block', 'create_block'):
            with self.subTest(outil=outil):
                registre = Registre()
                registre.ajouter(outil, {}, ToolResult(success=True, data={
                    'par_le_code': True, 'decision_code': 'execute', 'cle_demande': 'd1'}))
                with patch('services.agent_v2.agent._charger_tour_decide', return_value=lambda *a: True):
                    self.assertFalse(self.agent._tour_decide(registre, 'Réponse à la question'))

    def test_la_demande_en_attente_reste_dans_son_chat_et_son_tour(self):
        from services.agent_v2.demandes import demandes_en_attente
        source = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                    role='user', content='Une demande')
        demande = {'cle': 'd1', 'emise_le': timezone.now().isoformat(), 'options': []}
        ConversationMessage.objects.create(user=self.user, conversation=self.chat,
            role='assistant', content='Une question', metadata={
                'en_reponse_a': source.pk, 'demandes': [demande]})
        courant = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                     role='user', content='Une réponse')
        # L'autre chat et une requête ultérieure ne doivent modifier ni le tour ni ses preuves.
        for chat in (self.autre, self.chat):
            ConversationMessage.objects.create(user=self.user, conversation=chat,
                                               role='assistant', content='Message ultérieur')
        jeton = contexte.portee_courante.set(contexte.PorteeConversation(
            self.user.pk, self.chat.pk, courant.pk))
        self.addCleanup(contexte.portee_courante.reset, jeton)
        self.assertEqual(demandes_en_attente(self.user), [demande])
        sources = contexte.contexte_verification(self.user, self.chat, courant.content, courant.pk)
        self.assertNotIn('Message ultérieur', json.dumps(sources))
        self.agent._exclu = courant.pk
        self.assertNotIn('Message ultérieur', str(self.agent._historique(self.user)))

    def test_source_de_reprise_cumulee_sans_acces_a_un_autre_chat(self):
        source = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                    role='user', content='Deux actions à faire')
        precision = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                       role='user', content='Une précision')
        assistant = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
            role='assistant', content='Une confirmation', metadata={
                'en_reponse_a': precision.pk, 'demande_source_id': source.pk})
        courant = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
                                                     role='user', content='Je confirme')
        self.assertEqual(contexte.demande_reprise(self.user, self.chat, courant)['id'], source.pk)
        source.conversation = self.autre
        source.save(update_fields=['conversation'])
        self.assertIsNone(contexte.demande_reprise(self.user, self.chat, courant))

    def test_formulaire_conserve_le_sens_des_champs_et_les_valeurs(self):
        from services.agent.tools import TOOL_MAP
        from services.agent_v2.reponse_question import consommer_reponse
        resultat = TOOL_MAP['ask_question_custom'].execute(self.user, kind='form',
            question='Quelles heures ?', fields=[{'id': 'f1', 'type': 'time_range',
                'label': 'Heures de l’activité', 'question': 'Quelles heures ?'}])
        self.assertTrue(resultat.success, resultat.message)
        question = resultat.data['question_custom']
        ConversationMessage.objects.create(user=self.user, conversation=self.chat, role='assistant',
            content='Précise les heures.', metadata={'question_custom': question})
        courant = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
            role='user', content='Voici mes réponses')
        valeurs = {'f1': {'start': '15:00', 'end': '17:00'}}
        consommer_reponse(self.user, self.chat, courant, {'id': question['id'], 'values': valeurs})
        courant.refresh_from_db()
        champ = courant.metadata['reponse_validee']['champs'][0]
        self.assertEqual((champ['label'], champ['type'], champ['valeur']),
                         ('Heures de l’activité', 'time_range', valeurs['f1']))
        self.assertIn('Heures de l’activité', str(self.agent._historique(self.user)))
        self.assertIn('15:00', str(self.agent._historique(self.user)))
        self.assertIn('17:00', str(self.agent._historique(self.user)))

    def test_le_contexte_traverse_le_thread_de_choix(self):
        from services.agent_v2.outils import _sans_boucle
        portee = contexte.PorteeConversation(self.user.pk, self.chat.pk, 123)
        jeton = contexte.portee_courante.set(portee)
        self.addCleanup(contexte.portee_courante.reset, jeton)
        async def lire():
            return contexte.portee_courante.get()
        async def simuler_boucle_existante():
            return _sans_boucle(lire)
        self.assertEqual(asyncio.run(simuler_boucle_existante()), portee)

    def test_un_formulaire_invalide_ne_perd_pas_la_demande_source(self):
        from services.agent.tools import TOOL_MAP
        from services.agent_v2.conversation import AgentConversation
        origine = ConversationMessage.objects.create(user=self.user, conversation=self.chat,
            role='user', content='Planifie cette activité et ajoute une autre tâche.')
        resultat = TOOL_MAP['ask_question_custom'].execute(self.user, kind='form', question='À quelle heure ?',
            fields=[{'id': 'heure', 'type': 'time', 'label': 'Heure', 'question': 'À quelle heure ?'}])
        self.assertTrue(resultat.success)
        question = resultat.data['question_custom']
        ConversationMessage.objects.create(user=self.user, conversation=self.chat, role='assistant',
            content='À quelle heure ?', metadata={'en_reponse_a': origine.pk,
                'demande_source_id': origine.pk, 'question_custom': question})
        resultat = AgentConversation().process_message(self.user, 'Voici ma réponse.',
            conversation_id=self.chat.pk, question_answer={'id': question['id'], 'values': {'heure': '99:00'}})
        assistant = ConversationMessage.objects.get(pk=resultat['assistant_message_id'])
        self.assertTrue(resultat['question_posee'])
        self.assertEqual(assistant.metadata.get('demande_source_id'), origine.pk)

    def test_le_juge_partage_le_contexte_sans_nouvel_appel_et_sans_cache_croise(self):
        jugement._CACHE.clear()
        self.addCleanup(jugement._CACHE.clear)
        sources = {'historique': [{'role': 'user', 'texte': 'Contrainte A'}]}
        portee = contexte.PorteeConversation(self.user.pk, self.chat.pk, 1, sources)
        jeton = contexte.portee_courante.set(portee)
        self.addCleanup(contexte.portee_courante.reset, jeton)
        questions = {'sociale': jugement.q_interaction_sociale()}
        with patch.object(jugement, '_juger_sans_cache', return_value=(
                {'sociale': {'valeur': False, 'confiance': .99}}, 'test')) as appel:
            jugement.juger('Même réponse', questions)
            jugement.juger('Même réponse', questions)
            self.assertEqual(appel.call_count, 1)
            self.assertEqual(appel.call_args.args[0]['contexte_conversation'], sources)
            contexte.fixer_sources({'historique': [{'role': 'user', 'texte': 'Contrainte B'}]})
            jugement.juger('Même réponse', questions)
            self.assertEqual(appel.call_count, 2)

    def test_un_refus_n_occulte_pas_les_autres_occupations_fixes(self):
        from core.models import RecurringBlock
        from services.agent.tools.commun import _window_conflict
        jour = date(2030, 6, 10)
        for titre, debut, fin in [('A', time(9), time(9, 15)), ('B', time(9, 30), time(10))]:
            RecurringBlock.objects.create(user=self.user, title=titre, block_type='work',
                day_of_week=jour.weekday(), flexibility='fixed', start_time=debut, end_time=fin)
        resultat = _window_conflict(self.user, jour, 9 * 60, 10 * 60)
        self.assertFalse(resultat.success)
        self.assertEqual(resultat.data['fixed_conflicts'], [
            {'start_time': '09:00', 'end_time': '09:15'},
            {'start_time': '09:30', 'end_time': '10:00'}])
        self.assertFalse(resultat.data['conflict_is_exhaustive'])


class RepriseReelleTests(TransactionTestCase):
    """Runtime et base réels; seul le fournisseur de texte est scripté."""

    def test_confirmation_reprend_la_seconde_action_sans_reexecuter_la_premiere(self):
        self._exercer_reprise('confirmer')

    def test_refus_conserve_la_cible_et_n_abandonne_pas_une_action_independante(self):
        self._exercer_reprise('annuler')

    def _exercer_reprise(self, option):
        from pydantic_ai.models.function import FunctionModel, DeltaToolCall
        from core.models import Task
        from services.agent_v2.conversation import AgentConversation
        from services.agent_v2 import agent as module_agent
        from core.test_agent_v2_jugement import juger_script

        user = User.objects.create_user(username='reprise-reelle')
        chat = Conversation.objects.create(user=user)
        ancien = Task.objects.create(user=user, title='Ancienne activité')
        appels = []

        def juge(etat, questions):
            verdicts = juger_script({})(etat, questions)
            if 'reponse_etayee' in questions:
                self.assertTrue(all(a['etat_verifie'] for a in etat['resultats_et_recus'] if a['mutation']))
                verdicts['reponse_etayee'] = {'statut': 'decision', 'valeur': 'conforme', 'confiance': .99}
            return verdicts

        async def fournisseur(messages, info):
            appels.append(messages)
            n = len(appels)
            if n == 1:
                yield {0: DeltaToolCall(name='delete_task',
                    json_args=json.dumps({'task_id': ancien.pk}), tool_call_id='suppression')}
            elif n == 2:
                yield 'J’attends ta confirmation.'
            elif n == 3:
                # La source et le résultat doivent atteindre le vrai graphe du SDK.
                texte = str(messages)
                self.assertIn('Nouvelle activité', texte)
                if option == 'confirmer':
                    self.assertIn('RÉSULTATS DES OUTILS', texte)
                    self.assertIn('delete_task', texte)
                else:
                    self.assertIn('REFUSE', texte)
                yield {0: DeltaToolCall(name='create_task',
                    json_args=json.dumps({'title': 'Nouvelle activité'}), tool_call_id='creation')}
            else:
                yield 'La demande est traitée.'

        with patch.object(module_agent, 'modele_agir', return_value=FunctionModel(stream_function=fournisseur)), \
             patch.object(module_agent.PlannerAgentV2, '_voie_rapide_sociale', return_value=False), \
             patch('services.agent_v2.jugement.juger', side_effect=juge):
            premier = AgentConversation().process_message(user,
                'Supprime Ancienne activité et crée Nouvelle activité.', conversation_id=chat.pk)
            self.assertTrue(Task.objects.filter(pk=ancien.pk).exists())
            chip = next(c for c in premier['quick_replies'] if c.get('option') == option)
            second = AgentConversation().process_message(user, chip['value'], conversation_id=chat.pk,
                tap={'demande': chip['demande'], 'option': chip['option']})

        self.assertEqual(Task.objects.filter(pk=ancien.pk).exists(), option == 'annuler')
        self.assertEqual(Task.objects.filter(user=user, title='Nouvelle activité').count(), 1)
        self.assertEqual(len(appels), 4)
        self.assertEqual(second['response'].strip(), 'La demande est traitée.')
        assistant = ConversationMessage.objects.get(pk=second['assistant_message_id'])
        self.assertEqual(assistant.metadata['demande_source_id'],
            ConversationMessage.objects.filter(user=user, role='user').order_by('pk').first().pk)
        self.assertIsNone(contexte.portee_courante.get())
