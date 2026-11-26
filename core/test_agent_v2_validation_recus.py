"""La prose validée ne peut pas dépasser ses preuves relues."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase

from services.agent.tools import execute_tool
from services.agent.tools.base import ToolResult
from services.agent_v2.registre import Registre
from services.agent_v2.reconciliation import reconcilier
from services.agent_v2.redaction import ReponseDire, composer
from services.agent_v2.mesure import verifier_prose
from services.agent_v2 import validation_recus as validation


class ValidationRecusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='preuves-prose')
        self.registre = Registre()
        self.params = {'title': 'Action de test'}
        self.resultat = execute_tool('create_task', self.user, self.params)
        self.assertTrue(self.resultat.success, self.resultat.message)
        self.registre.ajouter('create_task', self.params, self.resultat)
        self.texte = 'La tâche Action de test est créée.'
        self.verdict = {'statut': 'decision', 'valeur': 'conforme', 'confiance': .99}

    def test_un_juge_ne_certifie_pas_un_succes_sans_relecture(self):
        self.assertIsNone(validation.etat_a_verifier(self.texte, self.registre))
        validation.enregistrer(self.registre, self.texte, self.verdict)
        self.assertFalse(validation.est_attestee(self.registre, self.texte))

    def test_la_phrase_validee_est_rendue_intacte_apres_relecture(self):
        reconcilier(self.user, self.registre)
        self.assertIsNotNone(validation.etat_a_verifier(self.texte, self.registre))
        validation.enregistrer(self.registre, self.texte, self.verdict)
        propre, coupes, _ = verifier_prose(ReponseDire(ouverture=self.texte), self.registre)
        self.assertEqual(coupes, 0)
        self.assertEqual(composer(propre, self.registre, '', None).prose, self.texte)
        self.assertFalse(validation.est_attestee(self.registre, self.texte + ' Et tout le reste aussi.'))
        self.registre.ajouter('delete_task', {}, ToolResult(success=False, message='Échec'))
        self.assertFalse(validation.est_attestee(self.registre, self.texte))

    def test_le_juge_incertain_ne_certifie_pas_la_phrase(self):
        reconcilier(self.user, self.registre)
        for statut in ('indisponible', 'incertain'):
            validation.enregistrer(self.registre, self.texte, {**self.verdict, 'statut': statut})
            self.assertFalse(validation.est_attestee(self.registre, self.texte))

    def test_les_preuves_trop_longues_ne_sont_pas_tronquees_pour_approbation(self):
        reconcilier(self.user, self.registre)
        self.assertIsNone(validation.etat_a_verifier('x' * 16001, self.registre))


class RetourDansLaBoucleTests(TransactionTestCase):
    def _tour_refuse(self, statut, valeur):
        from pydantic_ai.models.function import FunctionModel, DeltaToolCall
        from services.agent_v2.conversation import AgentConversation
        from services.agent_v2 import agent as module_agent
        from core.test_agent_v2_jugement import juger_script
        user = User.objects.create_user(username='refus-borne')
        appels = []

        async def fournisseur(messages, info):
            appels.append(messages)
            if len(appels) == 1:
                yield {0: DeltaToolCall(name='create_task', json_args='{"title":"Action A"}', tool_call_id='ajout')}
            else:
                yield 'Tous tes événements ont été supprimés.'

        def juge(etat, questions):
            reponses = juger_script({})(etat, questions)
            if 'reponse_etayee' in questions:
                reponses['reponse_etayee'] = {'statut': statut, 'valeur': valeur, 'confiance': .99}
            return reponses

        with patch.object(module_agent, 'modele_agir', return_value=FunctionModel(stream_function=fournisseur)), \
             patch.object(module_agent.PlannerAgentV2, '_voie_rapide_sociale', return_value=False), \
             patch('services.agent_v2.jugement.juger', side_effect=juge):
            resultat = AgentConversation().process_message(user, 'Crée la tâche Action A.')
        self.assertNotIn('Tous tes événements', resultat['response'])
        self.assertIn('Action A', resultat['response'])
        from core.models import Task
        self.assertEqual(Task.objects.filter(user=user).count(), 1)
        return len(appels)

    def test_jugement_indisponible_utilise_les_recus_sans_reformulations_payantes(self):
        self.assertEqual(self._tour_refuse('indisponible', None), 2)

    def test_rejet_persistant_est_borne_et_ne_publie_pas_la_phrase_rejetee(self):
        self.assertEqual(self._tour_refuse('decision', 'contredit'), 4)

    def test_un_rejet_revient_au_modele_qui_peut_relire_avec_un_outil(self):
        from pydantic_ai.models.function import FunctionModel, DeltaToolCall
        from services.agent_v2.conversation import AgentConversation
        from services.agent_v2 import agent as module_agent
        from core.test_agent_v2_jugement import juger_script
        user = User.objects.create_user(username='validation-boucle')
        appels, controles = [], []

        async def fournisseur(messages, info):
            appels.append(messages)
            n = len(appels)
            if n == 1:
                yield {0: DeltaToolCall(name='create_task', json_args='{"title":"Action A"}', tool_call_id='ajout')}
            elif n == 2:
                yield 'Toutes tes tâches sont terminées.'
            elif n == 3:
                self.assertIn('VALIDATION DE LA RÉPONSE', str(messages))
                yield {0: DeltaToolCall(name='list_tasks', json_args='{}', tool_call_id='lecture')}
            else:
                yield 'La tâche Action A est créée. Elle reste à faire.'

        def juge(etat, questions):
            reponses = juger_script({})(etat, questions)
            if 'reponse_etayee' in questions:
                controles.append(etat)
                reponses['reponse_etayee'] = {'statut': 'decision', 'confiance': .99,
                    'valeur': 'contredit' if len(controles) == 1 else 'conforme'}
            return reponses

        with patch.object(module_agent, 'modele_agir', return_value=FunctionModel(stream_function=fournisseur)), \
             patch.object(module_agent.PlannerAgentV2, '_voie_rapide_sociale', return_value=False), \
             patch('services.agent_v2.jugement.juger', side_effect=juge):
            resultat = AgentConversation().process_message(user, 'Crée la tâche Action A.')
        self.assertEqual(len(appels), 4)
        self.assertEqual(resultat['response'].strip(), 'La tâche Action A est créée. Elle reste à faire.')
        from core.models import Task
        self.assertEqual(Task.objects.filter(user=user).count(), 1)
        self.assertFalse(Task.objects.get(user=user).completed)
