"""Le vrai SDK reprend après un outil, sans rejouer ses effets après timeout."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.models.fallback import FallbackModel

from services.agent_v2.agent import PlannerAgentV2
from services.agent_v2.budget_tour import ModeleBorne, modele_borne


class RepriseApresOutilTests(SimpleTestCase):
    def scenario(self, *, delai):
        effets, tentatives = [], []

        def creer_rappel() -> str:
            effets.append('rappel-1')
            return 'rappel-1 enregistré'

        def reponse(messages, info):
            recus = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not recus:
                return ModelResponse(parts=[ToolCallPart('creer_rappel', {}, 'appel-1')])
            tentatives.append(len(recus))
            if len(tentatives) == 1:
                raise TimeoutError()
            return ModelResponse(parts=[TextPart('Rappel enregistré.')])

        agent = Agent(FunctionModel(reponse), tools=[creer_rappel])
        with patch('services.agent_v2.retry.delai_avant_reessai', return_value=0):
            resultat = PlannerAgentV2._executer_boucle(agent, 'Crée un rappel', delai=delai)
        self.assertEqual(resultat.output, 'Rappel enregistré.')
        self.assertEqual(effets, ['rappel-1'], 'Le retry doit conserver le reçu et ne pas rejouer l’outil')

    def test_timeout_conserve_recu_en_async(self):
        self.scenario(delai=5)

    def test_timeout_conserve_recu_en_sync(self):
        self.scenario(delai=None)

    def test_timeout_sans_message_est_reconnu(self):
        from services.agent_v2.retry import est_timeout
        self.assertTrue(est_timeout(TimeoutError()))

    def test_exception_de_code_ne_declenche_pas_dix_reprises(self):
        from services.agent_v2.retry import est_reessayable
        self.assertFalse(est_reessayable(TypeError('signature incorrecte')))

    def test_outil_sans_recu_ne_se_rejoue_pas(self):
        effets = []

        def creer_rappel() -> str:
            effets.append('écrit')
            raise TimeoutError()

        def reponse(messages, info):
            return ModelResponse(parts=[ToolCallPart('creer_rappel', {}, 'appel-1')])

        with patch('services.agent_v2.retry.delai_avant_reessai', return_value=0):
            with self.assertRaises(TimeoutError):
                PlannerAgentV2._executer_boucle(Agent(FunctionModel(reponse), tools=[creer_rappel]),
                                               'rappel', delai=5)
        self.assertEqual(effets, ['écrit'])

    def test_flux_interrompu_apres_un_outil_conserve_son_recu(self):
        effets, tentatives = [], []

        def creer_rappel() -> str:
            effets.append('rappel-1')
            return 'rappel-1 enregistré'

        async def fragments(messages, info):
            recus = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not recus:
                yield {0: DeltaToolCall(name='creer_rappel', json_args='{}', tool_call_id='appel-1')}
            else:
                tentatives.append(True)
                yield 'Rappel '
                if len(tentatives) == 1:
                    raise TimeoutError()
                yield 'enregistré.'

        async def evenements(ctx, flux):
            async for _ in flux:
                pass

        with patch('services.agent_v2.retry.delai_avant_reessai', return_value=0):
            resultat = PlannerAgentV2._executer_boucle(
                Agent(FunctionModel(stream_function=fragments), tools=[creer_rappel]),
                'rappel', delai=5, event_stream_handler=evenements)
        self.assertEqual(resultat.output, 'Rappel enregistré.')
        self.assertEqual(effets, ['rappel-1'])

    def test_timeout_est_borne_a_trois_essais_sans_fermer_le_client_entre_eux(self):
        appels = []
        client = SimpleNamespace(close=Mock())

        def panne(messages, info):
            self.assertFalse(client.close.called)
            appels.append(True)
            raise TimeoutError()

        modele = FunctionModel(panne)
        modele.client = client
        with patch('services.agent_v2.retry.delai_avant_reessai', return_value=0):
            with self.assertRaises(TimeoutError):
                PlannerAgentV2._executer_boucle(Agent(modele), 'bonjour', delai=5)
        self.assertEqual(len(appels), 3)
        client.close.assert_called_once()


class TimeoutDuModeleBorneTests(SimpleTestCase):
    def reservation(self, *args):
        return SimpleNamespace(delai=0.02, max_sortie=128, terminer=Mock())

    def test_echeance_locale_declenche_le_fournisseur_de_secours(self):
        appels = []

        async def lent(messages, info):
            appels.append('primaire')
            await asyncio.sleep(1)

        def secours(messages, info):
            appels.append('secours')
            return ModelResponse(parts=[TextPart('Réponse de secours')])

        with patch('services.agent_v2.budget_tour.AppelFournisseur', self.reservation):
            resultat = Agent(modele_borne(FallbackModel(
                FunctionModel(lent), FunctionModel(secours)))).run_sync('bonjour')
        self.assertEqual(resultat.output, 'Réponse de secours')
        self.assertEqual(appels, ['primaire', 'secours'])

    def test_fournisseur_lent_est_ecarte_du_reste_du_tour(self):
        appels = []

        async def lent(messages, info):
            appels.append('primaire')
            await asyncio.sleep(1)

        def secours(messages, info):
            appels.append('secours')
            return ModelResponse(parts=[TextPart('OK')])

        agent = Agent(modele_borne(FallbackModel(FunctionModel(lent), FunctionModel(secours))))
        with patch('services.agent_v2.budget_tour.AppelFournisseur', self.reservation):
            agent.run_sync('question 1')
            agent.run_sync('question 2')
        self.assertEqual(appels, ['primaire', 'secours', 'secours'])

    def test_delai_stream_avant_premier_fragment_bascule(self):
        async def lent(messages, info):
            await asyncio.sleep(1)
            yield 'trop tard'

        async def secours(messages, info):
            yield 'secours'

        async def evenements(ctx, flux):
            async for _ in flux:
                pass

        with patch('services.agent_v2.budget_tour.AppelFournisseur', self.reservation):
            resultat = asyncio.run(Agent(modele_borne(FallbackModel(
                FunctionModel(stream_function=lent), FunctionModel(stream_function=secours)
            ))).run('bonjour', event_stream_handler=evenements))
        self.assertEqual(resultat.output, 'secours')

    def test_secours_en_milieu_du_flux_recoit_les_resultats_deja_acquis(self):
        effets = []

        def creer_rappel() -> str:
            effets.append('rappel-1')
            return 'rappel-1 enregistré'

        async def primaire(messages, info):
            recus = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not recus:
                yield {0: DeltaToolCall(name='creer_rappel', json_args='{}', tool_call_id='appel-1')}
            else:
                yield 'Brouillon interrompu'
                await asyncio.sleep(1)

        async def secours(messages, info):
            recus = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            self.assertEqual(len(recus), 1)
            self.assertEqual(recus[0].content, 'rappel-1 enregistré')
            yield 'Rappel enregistré.'

        async def evenements(ctx, flux):
            async for _ in flux:
                pass

        with patch('services.agent_v2.budget_tour.AppelFournisseur', self.reservation), \
             patch('services.agent_v2.retry.delai_avant_reessai', return_value=0):
            agent = Agent(modele_borne(FallbackModel(
                FunctionModel(stream_function=primaire), FunctionModel(stream_function=secours)
            )), tools=[creer_rappel])
            resultat = PlannerAgentV2._executer_boucle(agent, 'rappel', delai=5,
                                                     event_stream_handler=evenements)
        self.assertEqual(resultat.output, 'Rappel enregistré.')
        self.assertEqual(effets, ['rappel-1'])
