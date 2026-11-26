# Contraintes de planning : correction générale du 26 novembre 2026

## Problème et correction

Les outils fusionnaient les occupations pour calculer les disponibilités, puis essayaient de retrouver une activité correspondant à la plage fusionnée. Un trajet et une révision pouvaient ainsi devenir un bloc anonyme. Le modèle recevait un refus sans origine fiable et reconstruisait une explication.

`services/scheduling/constraints.py` conserve chaque composante avec son type, ses bornes, sa source et la règle appliquée. Une activité, son trajet, sa préparation et sa marge de sécurité restent identifiables. Les intervalles fusionnés servent seulement au calcul des disponibilités. La comparaison est semi-ouverte : une fin à 13 h ne chevauche pas une activité qui commence à 13 h. Un transport peut cependant occuper le temps précédent.

Les refus sont renvoyés au modèle avec toutes les contraintes intersectées. Aucune écriture n'est effectuée lors du refus. La réponse finale reste rédigée et diffusée par le modèle. Aucun filtre de prose ni juge systématique n'a été ajouté.

## Couverture des outils

| Surface | Contrat appliqué |
| --- | --- |
| `get_today_schedule`, `find_free_slots` | Contraintes datées et disponibilités calculées à partir des mêmes données. |
| `get_week_schedule`, `list_blocks` | Activités séparées des marges et des politiques de réservation. La somme des durées d'activité est explicitement distinguée des marges. |
| `schedule_task_at` | Validation de l'activité et de ses propres marges, sans création partielle lors d'un conflit; conservation d'un ancien créneau si son déplacement échoue. |
| `create_block`, `update_block` | Comparaison des récurrences, de leurs bornes, exceptions et remplacements datés, ainsi que des tâches ponctuelles. Les marges des deux éléments comptent. |
| `replace_block_occurrence`, `restore_block_occurrence` | Cause conservée; annulation transactionnelle du remplacement refusé; rétablissement refusé s'il recouvre une réservation créée entre-temps. |
| `organize_day`, `optimize_week`, `check_feasibility` | Solveurs alimentés par les mêmes occupations et retour des sources des contraintes. |
| `detect_conflicts` | Fenêtre explicite de sept jours, occurrences effectives, tâches, sommeil et marges; deux éléments qui se touchent ne sont pas déclarés en conflit. |
| `suggest_schedule_optimization` | Analyse du planning daté effectif, y compris les tâches et les marges. |
| Planificateur automatique et API de blocs | Réutilisation du calcul partagé et des explications typées. |
| Suppressions, coches et exceptions | Les lectures recalculent les contraintes depuis l'état réel; aucun cache global susceptible de conserver une occupation supprimée. |

Les tâches sans créneau et les rappels ne réservent pas de temps. Leurs règles ne sont pas modifiées.

## Décision humaine et alternatives

Un résultat de placement bloqué porte `requires_user_decision`. La boucle v3 reconstruit cet état à partir du protocole persistant. Elle laisse les lectures accessibles, impose un appel d'outil et suspend les nouvelles écritures jusqu'à la réponse à `ask_question_custom`. La question reste rédigée par le modèle. La reprise lève cet état à partir de la réponse associée à cet appel précis. Une nouvelle demande indépendante et une conversation sociale ne déclenchent pas ce mécanisme.

Les premiers essais réels ont montré qu'une instruction dans le prompt ne suffisait pas : le modèle terminait parfois par une question en prose. Le contrat de décision corrige ce défaut sans rechercher de mots dans le texte. Un validateur de sortie contrôle cet état du protocole si un fournisseur ignore le choix d'outil requis.

Les alternatives doivent inclure les marges du nouvel élément. `find_free_slots` accepte `reservation_type=recurring` et un `task_id` pour calculer ces marges. Une fenêtre vérifiée sur une date est étiquetée comme telle; la création revalide la série. Une fin au début du transport du cours ne suffit pas si le nouveau bloc possède lui-même un trajet de retour.

## Vérification

Les comptes et données d'essai sont synthétiques. PostgreSQL utilise exclusivement `127.0.0.1:55439/planner_refonte`. Aucune donnée de planning de production n'a été modifiée.

- Première suite complète : 2 393 succès et quatre échecs. Les échecs exigeaient les anciens libellés, une plage fusionnée anonyme ou un mock de l'ancien helper. Les assertions ont été remplacées par des vérifications de faits et une vraie fixture de sommeil reporté.
- Suite complète après correction : 2 409 tests pytest réussis, quatre ignorés; 2 265 tests découverts par Django, résultat OK avec quatre ignorés.
- Derniers changements du contrat et des lectures : 212 tests ciblés réussis. La CI doit également exécuter l'ensemble sur le commit proposé.
- PostgreSQL : 77 tests réussis sur la version finale. Arrêt réel après commit puis reprise : un seul effet et un seul reçu. Adaptateur DBOSAgent réel : appel, reçu, diffusion, rejeu sans nouvelle consommation et purge vérifiés.
- DeepSeek réel : 24 scénarios réussis sur trois passages après ajout du contrat de décision. La matrice inclut explication, conflit, réponse différée, écriture vérifiée, horaires adjacents, récurrence, correction factuelle et question sociale. La dernière passe ajoute 16 scénarios réussis sur deux passages avec les derniers ajustements.
- Navigateur réel avec DeepSeek : question persistante après rechargement, réponse, reçu vérifié, fin du tour, conversation conservée, aucune erreur JavaScript et aucun débordement à 390 px. Les preuves et captures locales sont dans `docs/evaluations/2026-11-26/constraints/browser/`.
- Migrations : aucune migration manquante.

Commandes principales :

```powershell
$env:DEBUG='True'
$env:DATABASE_URL='sqlite:///:memory:'
.\.venv-refonte\Scripts\python.exe -m pytest -q
.\.venv-refonte\Scripts\python.exe manage.py test --noinput
.\.venv-refonte\Scripts\python.exe manage.py makemigrations --check --dry-run

$env:DATABASE_URL='postgresql://planner_test@127.0.0.1:55439/planner_refonte'
.\.venv-refonte\Scripts\python.exe manage.py test core.test_planning_constraints core.test_agent_runtime_concurrency --noinput
.\.venv-refonte\Scripts\python.exe scripts/check_runtime_recovery.py
.\.venv-refonte\Scripts\python.exe scripts/check_runtime_workflow.py
.\.venv-refonte\Scripts\python.exe scripts/evaluate_constraints.py --env-file ../planner_backend/.env --railway-relay --repeat 2 --output docs/evaluations/2026-11-26/constraints/live-release.json
```

Chaque commande est contrôlée séparément par son code de sortie. Le relais QA utilise une session Railway authentifiée, appelle DeepSeek avec TLS vérifié et n'ouvre jamais la base de production. Il transmet seulement les données synthétiques de l'essai. Son transport est tamponné : les durées ne représentent pas la latence du streaming en production.

## Erreurs rencontrées et limites

Le premier relais Windows passait par un shim `.CMD` et dépassait sa limite de ligne de commande. Le relais appelle maintenant le binaire natif. Le premier essai navigateur était invalide : la préparation des fixtures avait échoué faute de `DEBUG=True`. Les fixtures ont été recréées et vérifiées avant de rejouer le parcours.

Les premiers indicateurs automatiques validaient l'état et les écritures, sans contrôler suffisamment les alternatives. La lecture des réponses a révélé cette lacune. Les assertions incluent désormais les alternatives dangereuses reproduites; les traces restent conservées pour distinguer ces étapes.

Le texte diffusé en direct conserve un risque d'imprécision, accepté dans la refonte. Les résultats prouvés concernent les scénarios exécutés et les invariants des outils; ils ne constituent pas une garantie de compréhension parfaite de toute formulation. Les conflits sont revalidés avant écriture. Le correctif ne modifie pas la politique de confirmation destructive.

## Références

- Cerveau : `04-systemes/regles/planner-resultat-outil-vers-modele-avant-reponse.md`, appliquée en renvoyant les erreurs au modèle; avancement et erreurs consignés dans `03-daily/2026-11-26.md`.
- [DeepSeek, choix d'outil et restriction du mode thinking](https://api-docs.deepseek.com/api/create-chat-completion/), consulté le 26 novembre 2026. Le choix requis est utilisé sans thinking.
- [PydanticAI, contrôle dynamique des outils](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/), consulté le 26 novembre 2026. L'implémentation utilise les paramètres disponibles dans la version épinglée 1.30.1, inspectée localement.

Déploiement : aucun déploiement de ce correctif effectué à la rédaction de cette note.
