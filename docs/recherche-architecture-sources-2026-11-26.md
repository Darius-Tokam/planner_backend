# Recherche primaire : architecture candidate pour Planner

Recherche du 26 novembre 2026. Ce document compare des mécanismes observés dans les sources, pas des performances mesurées de Planner. Aucun code produit, modèle configuré ou déploiement n'a été modifié. Le code OpenCode a été téléchargé et lu; son application n'a pas été exécutée.

## Conclusion de recherche

La base candidate la plus cohérente est une boucle unique où le modèle lit, appelle les outils et reçoit leurs résultats, entourée de services métier déterministes et d'une session persistante. Pour l'exigence de reprise automatique après crash et d'attente humaine durable, le premier prototype recommandé associe **PydanticAI, DBOS et PostgreSQL**, avec Django conservé pour le métier. La session, les autorisations, les opérations et les interactions en attente doivent exister indépendamment du contexte envoyé au modèle et de la connexion SSE.

Cette conclusion ne justifie pas de recopier OpenCode, de passer automatiquement à LangGraph ou de remplacer Django. Elle justifie de comparer une nouvelle orchestration minimale à l'existante sur des conversations complètes. Un juge sémantique systématique de toute prose n'apparaît pas comme une obligation des architectures examinées. L'absence de ce juge ne garantit cependant pas qu'une phrase générée sera toujours vraie.

## OpenCode actuel : ce qui est effectivement dans le code

La branche par défaut distante est `dev`, vérifiée avec `git ls-remote --symref`. Révision téléchargée : `bcd14570020e0619aaf450bbe974898e12cadebb`. Le moteur actuel est sous `packages/core`, et non sous l'ancien `packages/opencode/src`. Les anciens décorticages de Planner basés sur une autre révision ne suffisent donc pas pour décrire cette version.

### Boucle et événements

`runner/llm.ts` recharge le contexte, lance une étape, puis continue selon le résultat de cette étape. `runner/step.ts` consomme le flux du fournisseur, publie ses événements et lance les outils locaux après la publication de leur appel. Les outils peuvent travailler en parallèle. Il attend leur terminaison avant de décider de poursuivre. Ce mécanisme n'est pas un pipeline obligatoire « classificateur, planificateur, exécuteur, juge, narrateur ».

Sources : [boucle](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/llm.ts#L219-L227), [étape et exécution des outils](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/step.ts#L135-L191).

Le texte est diffusé à partir de `text-delta`; les fragments sont regroupés avec un intervalle de 100 ms. Les appels et résultats d'outils ont leurs propres événements. Le chemin de publication étudié n'attend pas un juge général de vérité avant d'envoyer chaque réponse. Cela établit un choix d'implémentation, pas une preuve d'absence d'hallucinations. Les plugins peuvent aussi modifier le comportement.

Source : [publication du flux](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/publish-llm-event.ts#L100-L107), [fragments et appels](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/publish-llm-event.ts#L438-L550).

### Résultats et erreurs

L'historique reconstruit des paires d'appels et de résultats. Un outil terminé devient un résultat normal; un outil échoué devient un résultat de type erreur adressé au modèle. Après un flux interrompu, le code conserve les outils locaux effectivement exécutés et coupe les parties inachevées selon des règles explicites. Il ne déduit pas une réussite d'une phrase de l'assistant.

Source : [conversion de l'historique en messages modèle](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/to-llm-message.ts#L116-L178).

### Questions et permissions : une limite à ne pas copier

L'outil `question` fournit des questions structurées avec choix et réponse libre. La réponse revient au modèle avec le libellé de la question et sa valeur. C'est un vrai résultat d'outil; il n'est pas nécessaire de réinterpréter un texte générique « Voici mes réponses ».

Source : [outil question](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/tool/plugin/question.ts#L12-L143).

En revanche, dans cette révision, les formulaires en attente sont dans un cache mémoire et un `Deferred`. Les événements de formulaire sont explicitement éphémères. Les permissions en attente utilisent aussi une `Map`; seules les autorisations enregistrées passent par le stockage SQL. La fermeture annule les attentes. Il serait faux de présenter cette implémentation comme une garantie de reprise d'une question identique après redémarrage.

Sources : [formulaires en mémoire](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/form.ts#L105-L236), [événements éphémères](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/schema/src/form.ts#L173-L177), [permissions](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/permission.ts), [autorisations sauvegardées](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/permission/saved.ts).

**Conséquence pour Planner :** reprendre le contrat outil-question/résultat, mais persister la question, son schéma, son appel d'origine, son état et sa réponse dans PostgreSQL. Une application mobile doit pouvoir répondre après fermeture, reconnexion et redémarrage serveur.

### Persistance et reprise

Les événements de session et les transitions d'outils sont durables, tandis que les deltas texte sont éphémères et la fin du texte persistée. Le bus applique les projections dans une transaction avant publication. La session conserve aussi une déclaration durable de travail en cours. Au redémarrage, les appels restés inachevés peuvent être marqués interrompus. Cela ne fournit pas une transaction exactement une fois avec un effet extérieur arbitraire.

Sources : [événements de session](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/schema/src/session-event.ts#L408-L583), [bus transactionnel](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/bus.ts#L316-L454), [déclaration durable](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/store.ts#L75-L99), [appels interrompus](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/runner/llm.ts#L431-L453).

### Contexte

OpenCode distingue l'historique persistant de la fenêtre envoyée au modèle. Sa compaction conserve notamment demandes, contraintes, décisions et questions ouvertes; les messages récents priment sur l'ancien résumé. Le résumé peut être préparé hors du chemin critique. Il n'efface pas l'historique source du stockage.

Source : [compaction](https://github.com/anomalyco/opencode/blob/bcd14570020e0619aaf450bbe974898e12cadebb/packages/core/src/session/compaction.ts#L109-L245).

## PydanticAI : le SDK existant n'impose pas le blocage du texte

La documentation distingue `run_stream`, susceptible de conclure au premier résultat compatible, de `run(event_stream_handler=...)`, `run_stream_events` et `iter`, qui permettent de suivre la boucle et ses outils. Le flux d'événements expose début, fragments, appels et résultats. Planner peut donc transmettre du texte pendant une boucle complète sans choisir l'API qui s'arrête trop tôt.

Source : [streaming de tous les événements](https://pydantic.dev/docs/ai/core-concepts/agent/#streaming-all-events).

Les outils différés représentent les permissions humaines et les résultats fournis plus tard. La reprise transmet les résultats associés aux identifiants d'appels et l'historique conservé. La documentation actuelle propose plusieurs modes de résolution; sa version actuelle ne doit pas être confondue avec la dépendance installée dans Planner.

Source : [outils différés](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/).

Vérification locale réellement exécutée dans le venv de Planner : version **1.30.1**; `DeferredToolRequests`, `DeferredToolResults`, `ApprovalRequired`, `CallDeferred`, `Agent.run_stream_events` et `Agent.iter` existent. Cette introspection ne valide ni leur intégration à Django ni la reprise après crash. Elle montre qu'il n'est pas nécessaire de changer de framework simplement pour obtenir ces primitives.

La persistance des messages et l'exécution durable sont deux sujets distincts. Les intégrations durables actuelles proposent des moteurs comme Temporal ou DBOS, qui journalisent les opérations terminées pour les rejouer sans refaire leurs appels. Choisir un tel moteur ajoute une dépendance d'exploitation; cela ne corrige pas un mauvais contrat métier.

Source : [exécution durable](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/).

## LangGraph : utile pour la persistance, pas une réparation automatique de la compréhension

`interrupt()` suspend un graphe avec un état checkpointé; la reprise utilise le même `thread_id` et un `Command`. Un checkpointer mémoire ne survit pas au redémarrage : une installation de production exige un stockage durable. Lors de la reprise, le nœud interrompu recommence; les effets précédant l'interruption peuvent donc être réexécutés. La documentation demande de les rendre idempotents et de relire l'état extérieur après une longue attente.

Sources : [interruptions](https://docs.langchain.com/oss/python/langgraph/interrupts), [persistance](https://docs.langchain.com/oss/python/langgraph/persistence).

La documentation de tolérance aux pannes distingue aussi « accusé de réception absent » et « effet non réalisé ». Une clé d'idempotence doit être durable avant l'effet; si l'accusé est perdu, il faut vérifier l'état de l'opération. Changer de runtime ne supprime pas cette exigence.

Source : [frontières de retry et idempotence](https://docs.langchain.com/oss/python/langgraph/fault-tolerance#retry-boundaries-and-idempotency).

## DBOS : recommandation concrète pour éviter de réécrire un moteur durable

DBOS propose une intégration officielle avec Django et fonctionne comme bibliothèque dans le processus, avec état système en base. Son intégration PydanticAI transforme les exécutions d'agent en workflows et les requêtes modèle en étapes checkpointées. **Les outils personnalisés et handlers de streaming ne sont pas enveloppés automatiquement.** Leurs frontières durables doivent être définies explicitement. Le module d'intégration existe dans PydanticAI 1.30.1 installé; son fonctionnement avec une version choisie de DBOS reste à tester dans un environnement isolé.

Sources : [Django](https://docs.dbos.dev/integrations/django), [PydanticAI avec DBOS](https://docs.dbos.dev/integrations/pydantic-ai).

Métadonnées vérifiées : Python local `3.10.0`, `pydantic-ai-slim==1.30.1`, extra déclaré `dbos>=1.14.0`. Le [registre PyPI officiel](https://pypi.org/pypi/dbos/json) annonce DBOS `3.7.0`, publié le 24 novembre 2026, avec Python `>=3.10`. La borne Python convient, mais une dépendance sans plafond ne prouve pas la compatibilité de cette paire. Aucun paquet n'a été installé ou mis à jour. Le PoC doit figer les versions réellement testées.

La messagerie `send`/`recv` persiste les réponses; `recv(suspend=True)` permet de suspendre une attente longue sans conserver une exécution active. L'identifiant de workflow et la clé d'idempotence d'envoi servent à dédupliquer les reprises. Cela correspond au besoin de répondre à une question depuis l'iPhone après redémarrage du worker.

Source : [communication durable](https://docs.dbos.dev/python/tutorials/workflow-communication).

La garantie transactionnelle exactement une fois documentée concerne les **datasources SQLAlchemy de DBOS**, qui enregistrent le résultat dans la transaction SQL. Elle ne s'applique pas automatiquement aux écritures de l'ORM Django. Pour conserver ce dernier, l'outil doit enregistrer `operation_id`, mutation et reçu dans une même transaction Django avec contrainte d'unicité. Un step rejoué relit ce reçu. Pour les effets externes, il faut la déduplication du fournisseur ou la réconciliation d'un état inconnu.

Sources : [transactions et datasources](https://docs.dbos.dev/python/tutorials/transaction-tutorial), [étapes et reprises](https://docs.dbos.dev/python/tutorials/step-tutorial).

**Arbitrage :** un journal PostgreSQL de messages suffit si une interruption termine le tour et que l'utilisateur relance explicitement. Si le produit promet de reprendre automatiquement un travail en cours, le journal seul oblige à construire leases, wakeups, replay et migrations d'exécution. DBOS est donc le premier candidat, sous réserve d'un PoC. Ce choix retire du travail d'orchestration maison; il ne remplace pas le registre métier, les permissions ou les événements d'interface.

Le PoC doit tuer le worker après commit Django mais avant checkpoint DBOS, puis reprendre sans doublon; attendre une réponse humaine pendant un redémarrage; dédupliquer un double clic; reconstruire le flux après reconnexion; et reprendre une version de workflow après déploiement. Échec de ces contrôles : aucun déploiement de cette base. Temporal devient pertinent si l'exploitation exige des workflows distribués et une gestion de versions plus complexes; il n'est pas nécessaire d'ajouter Temporal et LangGraph à DBOS.

## Enseignements d'Anthropic, sans les transformer en preuve pour Planner

Le guide sur les agents recommande de commencer par la structure la plus simple qui résout le besoin, puis de justifier la complexité par les résultats. Les workflows prédéfinis servent des tâches prévisibles; une boucle outillée convient aux demandes variables. Une chaîne de modèles coûte latence et tokens. Ce principe ne prouve pas qu'un framework particulier sera plus rapide dans Planner.

Source : [Building effective agents](https://www.anthropic.com/research/building-effective-agents).

Le guide de contexte préconise un contexte réduit à l'information utile, des outils précis et des instructions à un niveau intermédiaire entre règles fragiles et consignes vagues. Cela appuie des skills chargés au besoin, des résultats compacts avec accès aux détails et un état de demande explicite. Cela n'autorise pas à éliminer les faits nécessaires à la demande active.

Source : [Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents).

L'architecture Managed Agents sépare trois interfaces : session persistante, boucle d'orchestration et exécution des outils. Le contexte du modèle reste une vue transformable de la session, pas la session elle-même. Anthropic publie des gains de latence sur sa propre infrastructure; aucun de ces pourcentages ne doit être repris comme prévision pour Planner.

Source : [Scaling Managed Agents](https://www.anthropic.com/research/managed-agents).

## Arbitrage recommandé pour le prototype

| Option | Intérêt pour Planner | Coût ou limite | Position |
| --- | --- | --- | --- |
| Garder toutes les couches et ajouter des gardes | Correctifs localement rapides | Accumulation des réinterprétations et contrôles | Référence de comparaison, pas cible présumée |
| Nouvelle boucle PydanticAI + DBOS + PostgreSQL | Primitives déjà disponibles et reprise durable prise en charge | Intégration, versionnage et idempotence Django à valider | Premier candidat à mesurer pour les besoins actuels |
| LangGraph avec PostgreSQL | Interruptions et checkpoints explicites | Nouvelle pile et discipline d'idempotence toujours nécessaire | Candidat si les workflows deviennent réellement complexes |
| Journal PostgreSQL sans moteur durable | Stockage et reprise explicite simples | Devient un moteur maison si l'on ajoute replay, leases et reprise automatique | Acceptable seulement si le contrat de reprise est réduit |
| Boucle entièrement maison sur API fournisseur | Peu de dépendances initiales | Parsing, compatibilité, reprise et tests à porter soi-même | Possible, sans supériorité démontrée |
| Copier OpenCode | Contrats utiles et source concrète | Moteur général volumineux, hypothèses de poste de développement | Copier les principes, pas le runtime entier |

Le prototype devrait d'abord couvrir création, ambiguïté, correction d'heure, action composée, suppression confirmée, refus, panne après écriture et reconnexion. Mesurer exactitude de l'état final, demandes répétées, effets non autorisés, appels modèle, tokens, délai du premier texte utile et délai total. Un simple affichage « je réfléchis » ne compte pas comme premier résultat utile.

Il reste un choix produit explicite : du texte libre diffusé immédiatement comporte un risque résiduel d'affirmation erronée; une validation sémantique complète avant affichage retarde nécessairement ce texte. Une carte d'état issue des reçus peut fournir une vérité fiable sur l'action sans faire réécrire toute la conversation par le code. Pour les cas critiques, on peut limiter la validation bloquante à la portée à risque, plutôt que la rendre universelle.

## Limites et reproductibilité

- Sources consultées et code OpenCode figé à la révision indiquée. Aucun benchmark comparatif des runtimes exécuté.
- Aucune preuve de qualité générale de DeepSeek ou de Jev produite par cette recherche.
- Introspection du SDK installé exécutée; aucun appel payant au fournisseur dans cette recherche.
- La proposition est une architecture candidate, à accepter ou rejeter sur les scénarios de Planner. Une nouvelle boucle ne rend pas un modèle infaillible.
- Fichiers OpenCode consultés dans `C:\Users\Darius\AppData\Local\Temp\opencode-architecture-20261126`. Clone sparse de la branche `dev`, vérifié par `git rev-parse HEAD`.
