# Diagnostic de la lenteur de Planner, 26 novembre 2026

## Périmètre et preuves

Production vérifiée sur `69700e4b9defc86a33ee01e690b89a8b32922291`, déploiement Railway `3b6d7e46-ab6c-4a17-852c-a02b0726bc02` en succès. Le correctif des formulaires d'octobre est déjà déployé.

Lecture des logs et des messages/metadata en PostgreSQL, transaction en lecture seule. Fenêtre du 26 novembre, 06 h 22 à 06 h 31 UTC, soit 01 h 22 à 01 h 31 à Toronto. Aucun message ajouté au chat et aucune modification du planning pendant le diagnostic.

| Demande, résumée | Durée du tour | Requêtes modèle | Délai dépassé |
| --- | ---: | ---: | --- |
| Prévoir un appel demain | 84,28 s | 7 | oui |
| Corriger la date pour aujourd'hui | 32,56 s | 4 | non |
| Choisir 11 h à 11 h 40 | 67,69 s | 4 | oui |
| Préciser l'heure d'ouverture du service | 87,07 s | 5 | non |
| Remplacer une activité par l'appel | 93,55 s | 5 | oui |
| Reprendre l'ajout après confirmation | 82,10 s | 8 | oui |

Moyenne : 74,54 s. Cinq tours sur six dépassent une minute. La confirmation traitée uniquement par le code prend 0,43 s.

Les outils observés prennent environ 0,2 à 150 ms. Les jugements Jev examinés prennent environ 69 à 118 ms. Le temps se concentre dans les appels du modèle principal : certains réussissent après 18 ou 49 secondes, quatre expirent après 60 secondes. Plusieurs étapes supplémentaires servent à corriger une réponse ou un appel de question invalide.

## Défauts reproduits et correction

1. **Le délai local contourne le secours prévu.** `ModeleBorne` lève `TimeoutError`, alors que le `FallbackModel` installé intercepte par défaut `ModelAPIError`. La correction conserve la cause originale et utilise une erreur reconnue par le secours.
2. **La reprise repart du message initial.** `_executer_boucle` ne récupérait pas les messages du tour échoué. Le modèle perdait les résultats déjà obtenus. Un test avec le vrai graphe PydanticAI et un fournisseur local reproduit deux exécutions du même outil. La reprise conserve maintenant les reçus via `capture_run_messages`, sans ajouter une nouvelle demande utilisateur.
3. **Les clients étaient fermés entre deux tentatives.** La tentative suivante réutilisait ces clients. Les traces montrent des échecs DeepSeek en 20 à 60 ms après le premier timeout, puis des réponses du secours. La fermeture se fait désormais une seule fois, à la fin.
4. **Le compteur de timeouts inspectait seulement leur texte.** Une exception `TimeoutError()` vide n'était pas reconnue. Le contrôle utilise aussi les types et les causes. Les erreurs de code ne sont plus réessayées par défaut.
5. **Chaque étape pouvait refaire la même longue attente.** `AGENT_MODELE_DELAI` borne désormais chaque appel de planification à 25 s par défaut. Dans une chaîne de secours, le fournisseur qui dépasse ce délai est écarté pour le reste de cette instance de tour, avant toute nouvelle provision.

Les reçus ne sont conservés que si le graphe a atteint une requête modèle. Si un outil échoue après avoir potentiellement écrit, sans rendre de reçu, la boucle ne le relance pas automatiquement. L'échéance globale et les compteurs partagés restent actifs. Les modèles configurés, les autorisations et les confirmations ne changent pas.

Le SDK utilisé est PydanticAI 1.30.1. Le mécanisme de récupération d'historique a été vérifié dans son code installé et confronté à la [documentation des erreurs de modèle](https://pydantic.dev/docs/ai/core-concepts/agent/#model-errors). Le repli au milieu d'un flux ne bénéficie pas directement du secours du SDK : la reprise conservant les reçus permet alors de demander au fournisseur suivant de continuer.

## Qualité des réponses : constats distincts

- Au premier tour, les lectures visent d'abord le 27 novembre, puis le 30 après la reprise. Le texte continue pourtant à parler de « demain ». La divergence est constatée; attribuer toute erreur de date au seul retry serait excessif.
- Le modèle transforme une heure d'ouverture fournie par l'utilisateur en une plage d'ouverture supposée. Il propose ensuite à nouveau un créneau le soir. Aucune heure de fermeture n'a été fournie dans cette séquence.
- Le créneau demandé rencontre plusieurs occupations. Le premier refus et la confirmation ne présentent pas toute la résolution à effectuer. Après la suppression confirmée, la création restante n'est pas poursuivie dans ce tour, puis un autre obstacle est présenté.
- Certains tours cumulent une réponse en prose, un rejet de validation, un appel de question invalide et un deuxième appel de question. Une validation de forme réussie ne prouve pas que le choix présenté est utile.

Ces problèmes ne sont pas déclarés corrigés par ce lot. La suite doit rejouer des conversations sur un compte de test, contrôler les faits proposés et les modifications finales, et comparer temps, tokens et nombre de relances. Les critères sont généraux : préserver une correction utilisateur, ne pas inventer une contrainte externe, présenter les conflits pertinents ensemble et conserver les actions restantes après confirmation. Il ne faut pas ajouter une règle spéciale pour une banque ou une phrase précise.

## Usage et limites de preuve

Les métadonnées examinées enregistrent plusieurs appels réussis avec `usage_connu=false`, ce qui conserve leurs provisions dans le garde de coût. Ce ne sont pas des consommations mesurées. Trois petites sondes isolées ont vérifié successivement le flux DeepSeek brut, le SDK et son enveloppe de budget avec admission simulée : chacune a reçu 9 tokens d'entrée et 2 de sortie. La première réponse est arrivée en 1,52 s. Aucun outil métier n'a été exécuté. Ces sondes prouvent que les compteurs peuvent remonter aujourd'hui, pas pourquoi ils manquaient dans les conversations antérieures ni que le cas complet est rapide.

Validation locale : tests de la vraie boucle et de ses outils, fournisseurs scriptés, SQLite de test. Les scénarios couvrent reprise normale et streaming, panne après effet sans reçu, limite de tentatives, fermeture des clients, secours avant et pendant un flux, budgets et questions. Le test initial avait quatre échecs et une erreur avant correction. Le lot ciblé final compte 135 tests.

Commande rejouable, depuis ce worktree avec le Python du projet, `DEBUG=True` et `DATABASE_URL` vide :

```powershell
python manage.py test core.test_agent_v2_reprise core.test_usage_retry core.test_agent_v2_delai core.test_agent_v2_modeles core.test_correction_budget core.test_agent_v2_boucle core.test_agent_v2_reponse_finale core.test_agent_v2_questions core.test_ask_question_custom core.test_agent_v2_formulaire_heures --noinput -v 1
```

La suite complète et la latence après déploiement ne sont pas validées par ces tests ciblés. Le seuil de 25 s est un délai par appel, pas une promesse de durée totale. Le correctif doit être revu avant fusion. Aucun déploiement effectué pendant ce diagnostic.
