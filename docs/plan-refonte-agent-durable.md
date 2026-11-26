# Refonte du moteur Planner

Spec: `docs/architecture-planner-recommandee-2026-11-26.md`.
Décision utilisateur: texte en direct, actions vérifiées, le 26 novembre 2026.

## Global Constraints

- Préserver les services métier, pièces jointes, quotas mesurés et clients existants.
- Un moteur par conversation. Aucun déploiement ou basculement de production dans ce mandat.
- Une boucle PydanticAI, DBOS pour les reprises; PostgreSQL réel pour les tests de crash et concurrence.
- Aucun routeur lexical d'intention, aucune réécriture bloquante de la prose après génération.
- Chaque résultat et erreur d'outil retourne au modèle. Permissions, consentement, portée et reçus restent contrôlés côté serveur.
- Tests exécutés avant affirmation; environnement isolé et aucun compte de production modifié.

### Task 1: Persistance et preuve DBOS

Interfaces: Conversation.engine_version; AgentRun, AgentInvocation, AgentOperation, AgentInteraction, AgentEvent.

1. Écrire des tests négatifs d'identité, isolation, ordre des événements, validation de réponse et reçu atomique. Expected: échec avant implémentation.
2. Ajouter modèles, migrations et services de persistance. Expected: les mêmes tests passent.
3. Exécuter DBOS avec PostgreSQL et un arrêt après commit métier, avant acquittement. Expected: un seul effet et récupération du reçu au redémarrage.
4. Enregistrer versions et commandes reproductibles. Gate: `python manage.py test core.tests.test_agent_runtime_persistence`.

### Task 2: Outils et autorisations

Interfaces: AgentOperation et AgentInteraction de Task 1; ToolResult existant et schémas métier.

1. Écrire les tests d'outils, refus, cibles modifiées, répétition et injection de paramètres privés. Expected: échecs ciblés.
2. Exposer les schémas complets; sérialiser les mutations; conserver effet et reçu ensemble; vérifier les postconditions; traiter les effets externes sans retry aveugle.
3. Lier le consentement aux arguments, cibles et expiration. Lier toute réponse humaine au tool_call_id.
4. Gate: `python manage.py test core.tests.test_agent_runtime_tools`. Expected: aucun effet sans autorisation, chaque erreur exploitable par le modèle.

### Task 3: Boucle, contexte et usage

Interfaces: outils Task 2; invocation immuable et événements Task 1; budget quotidien existant.

1. Écrire des scénarios PydanticAI contrôlés: salutation, lecture, écriture, clarification, refus, erreur récupérable, pause/reprise. Expected: échecs avant moteur.
2. Implémenter une boucle durable, texte immédiat, contexte typé borné, préfixe stable, skills et configuration modèle explicite.
3. Rapprocher les mesures fournisseur une seule fois par tentative réelle; aucune facturation supplémentaire lors du replay d'un résultat enregistré.
4. Gate: `python manage.py test core.tests.test_agent_runtime_loop`. Expected: continuité des outils et streaming sans juge bloquant.

### Task 4: API et reprise du flux

Interfaces: run/invocation/interaction Task 1, moteur Task 3; conversation.engine_version.

1. Écrire les tests API d'accès, idempotence, documents, réponses invalides, reconnexion et terminal persistant. Expected: échecs ciblés.
2. Ajouter admission, consultation et flux SSE ordonné; worker durable distinct du cycle HTTP; aiguillage par version de conversation.
3. Garder les conversations existantes sur leur moteur et prévoir activation contrôlée/retour arrière.
4. Gate: `python manage.py test core.tests.test_agent_runtime_api`. Expected: reprise après cursor sans fragment doublé ni double écriture.

### Task 5: Interface chat

Interfaces: contrats Task 4; frontend `origin/main` isolé.

1. Écrire les tests du réducteur d'événements et des interactions. Expected: échecs avant intégration.
2. Afficher texte progressif, cartes de reçus, questions persistantes, erreurs précises et reconnexion; garder notifications, documents et invalidation du planning.
3. Vérifier responsive et états des boutons. Gate: Vitest, TypeScript et build. Expected: verts; observation navigateur si environnement disponible.

### Task 6: Évaluations, revue et livraison

Interfaces: toute la refonte et services métier conservés.

1. Corpus versionné d'au moins 40 scénarios avec paraphrases réservées et cas négatifs. Distinguer simulation fournisseur et appels réels.
2. Mesurer latence premier texte/fin, usage, cache, appels, réussite avec réglages modèle comparables. Aucun résultat simulé présenté comme qualité du fournisseur.
3. Exécuter suites complètes backend/frontend et tests PostgreSQL de crash, concurrence, rollback.
4. Une revue indépendante finale; corriger les problèmes importants avec reproductions RED puis GREEN.
5. Livrer rapport, commandes, limites, migration/activation/rollback et branches vérifiables. Expected: aucun déploiement implicite.

## Review Focus

Vérifier consentement forgé, identité d'opération en replay, changement de cible après approbation, fermeture du processus entre mutation et reçu, reprise d'une génération partielle, course entre deux réponses humaines, dérive de date, fuite entre comptes, double comptabilisation, troncature de contexte et compatibilité des anciens chats.
