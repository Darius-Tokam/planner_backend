# Exploiter le moteur de chat v3

Ce document prépare une activation. Cette branche ne déploie aucun service.

## Contrat

Le modèle produit sa prose en streaming. Les cartes d’action proviennent des reçus serveur.
La prose n’est pas une preuve d’écriture et ne subit pas de juge bloquant systématique.
Le serveur contrôle le compte, le consentement IA, les paramètres, les cibles et les confirmations.
Les résultats, erreurs et valeurs nulles retournent au modèle.

Chaque conversation conserve son `engine_version`. Les conversations existantes restent sur v2.
Les nouveaux clients annoncent `client_runtime: v3`; le serveur décide de l’activation.
L’ancienne route refuse une conversation v3 pour empêcher deux moteurs de modifier le même tour.

## Services et configuration

PostgreSQL est obligatoire pour le worker. Le serveur HTTP et le worker utilisent la même base.
Les migrations Django 0049 à 0051 créent l’état applicatif et la file de purge. DBOS initialise son schéma `dbos`.
Prévoir des sauvegardes de ces deux ensembles et limiter leur accès aux services concernés.

| Variable | Valeur initiale | Effet |
| --- | --- | --- |
| `PLANNER_RUNTIME_DEFAULT` | `v2` | Moteur des nouvelles conversations éligibles |
| `PLANNER_V3_USER_IDS` | vide | Identifiants de comptes autorisés pour une cohorte |
| `PLANNER_RUNTIME_MODEL` | `deepseek-flash` | Modèle du nouveau moteur seulement |
| `PLANNER_REASONING` | `disabled` | Mode évalué pour le chat courant |
| `PLANNER_RUNTIME_VERSION` | `v3-1` | Version DBOS compatible avec le code du workflow |
| `PLANNER_WORKER_ID` | `planner-v3-worker` | Identité de l’exécuteur DBOS |

Service Railway séparé : `planner-chat-worker`. Le dispatch de `railway.toml`
lance `python manage.py run_chat_worker` pour ce nom exact. Il utilise le même
code que le web, avec des références aux variables nécessaires du backend.
Commencer avec un seul réplica, concurrence globale de quatre workflows. Ne pas partager
une identité d’exécuteur entre plusieurs réplicas. Une modification incompatible de l’ordre
des étapes exige de conserver l’ancien worker pour ses workflows avant de retirer sa version.
Le service `send-reminders` reste distinct du worker de chat.

Ordre d’activation, après validation de Darius :

1. Sauvegarder PostgreSQL, appliquer `python manage.py migrate --noinput`.
2. Déployer backend et worker avec le même code et les clés fournisseur existantes en variables serveur.
3. Déployer le frontend compatible. Conserver `PLANNER_RUNTIME_DEFAULT=v2`.
4. Ajouter le compte de test à `PLANNER_V3_USER_IDS`, ouvrir une nouvelle conversation.
5. Vérifier texte progressif, reçu, question après rechargement, refus, fermeture puis réouverture et quota.
6. Suivre les logs HTTP et worker jusqu’au démarrage sain. Élargir la cohorte seulement après ces observations.

Un push sur main peut déclencher Railway ou Vercel. La création des branches et PRs ne vaut
pas autorisation de merge ni de déploiement. Toute modification Railway se fait par CLI.

## Retour arrière

Remettre `PLANNER_RUNTIME_DEFAULT=v2` et vider la cohorte arrête les nouvelles admissions v3
dans de nouvelles conversations. Cela ne convertit pas les conversations existantes.
Garder les endpoints et le worker compatibles pour terminer ou annuler les demandes v3 actives.
Ne jamais rejouer leur texte dans v2 : des effets peuvent déjà être confirmés.
Ne pas supprimer les tables, reçus ou checkpoints durant un rollback applicatif.

## Reprise, erreurs et confidentialité

Le client envoie un UUID stable puis lit les événements par curseur. Une reconnexion ne refait
pas le POST. L’admission persistée survit à la fermeture de la connexion HTTP.
L’attente humaine termine l’invocation; sa réponse crée une nouvelle invocation avec le même
appel d’outil et le contexte daté d’origine. Une clarification expire après 24 heures,
une confirmation après une heure. Une confirmation ne peut pas être donnée en texte libre.

Une écriture métier et son reçu sont transactionnels. Un replay retrouve ce reçu.
Une notification externe n’offre pas cette atomicité : une interruption après l’envoi et
avant l’acquittement devient un résultat inconnu, sans renvoi automatique.
« Accepté par le fournisseur » ne garantit pas l’affichage sur l’appareil.

L’usage fournisseur confirmé alimente le quota. Une tentative sans mesure garde sa protection
de coût séparée. Un résultat fournisseur enregistré puis rejoué ne compte pas deux fois.
Les erreurs distinguent quota mesuré et protection de coût, avec heure de reprise.

Les checkpoints contiennent le protocole modèle, y compris des données personnelles et, selon
le fournisseur, son raisonnement. Ils ne sont pas exposés par l’API publique. Les événements
client contiennent uniquement la prose publique, les outils, les questions et leurs états.
Supprimer une conversation ou un compte inscrit une demande de purge dans la même transaction.
Le worker annule puis supprime les workflows DBOS et leurs enfants. Une erreur conserve la demande
pour réessai. Le tombstone conserve seulement un UUID, un compteur, une date et une classe d’erreur;
des passages répétés éliminent les checkpoints qu’un processus déjà en vol pourrait écrire tardivement.
La purge dépend donc du worker actif. Surveiller `AgentWorkflowPurge.last_error` et `next_attempt_at`.
Définir séparément une durée de rétention des checkpoints des comptes actifs avant une ouverture générale.
L’accès DBOS doit rester interne. Aucun endpoint de diagnostic public ne doit renvoyer ces données.

Les pièces jointes v3 sont extraites sans modifier le planning. Le modèle reçoit le résultat ou
l’erreur d’extraction; `import_document` applique uniquement un import demandé. La permission IA
est relue avant le commit de l’extraction et avant l’outil d’import. Le chemin v2 reste compatible.

## Vérifications reproductibles

Environnement isolé, PostgreSQL local nommé `planner_refonte`, jamais une base de production :

```text
python manage.py makemigrations --check --dry-run
python manage.py test core.test_agent_runtime_persistence core.test_agent_runtime_tools core.test_agent_runtime_loop core.test_agent_runtime_api core.test_agent_runtime_concurrency --noinput
python scripts/check_runtime_recovery.py
python scripts/check_runtime_workflow.py
python scripts/evaluate_runtime.py --env-file <fichier local> --split all --concurrency 1 --output <rapport.json>
python scripts/evaluate_legacy.py --env-file <fichier local> --output <reference.json>
```

Les deux derniers scripts effectuent des appels payants réels sur des comptes synthétiques.
Ils ne chargent que la clé DeepSeek du fichier fourni. Les preuves de crash utilisent un
fournisseur contrôlé et le vrai DBOS/PostgreSQL : elles prouvent la reprise, pas la qualité du modèle.
La CI PostgreSQL exécute ces preuves sans clé IA. Les évaluations réelles restent manuelles.

Frontend : `npx vitest run`, `npx tsc --noEmit -p tsconfig.app.json`, `npm run build`.
Le script `scripts/check-runtime-browser.mjs` utilise Edge headless et les serveurs QA locaux
8017 et 5179, préparés par `scripts/serve_runtime_qa.py`. Il ne teste pas TestFlight ni APNs.
`PLANNER_QA_CONTROLLED=1`, uniquement dans ces scripts QA, remplace le fournisseur par une fixture
de protocole pour tester le transport sans réseau. Le rapport indique alors `provider: controlled`.
Ce mode ne constitue jamais une mesure de compréhension ou de latence DeepSeek.
