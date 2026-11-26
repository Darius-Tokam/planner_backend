# Continuité et réponses de Planner : correction du 26 novembre 2026

Les échanges de production examinés perdaient des dates et des actions restantes, inventaient une contrainte extérieure et terminaient une demande après sa seule suppression confirmée. Le correctif traite les mécanismes communs. Il n'ajoute ni liste de formulations utilisateur, ni routage par mots-clés, ni règle particulière à une activité.

Les premiers tests ont reproduit trois défauts avant correction : contexte lu dans un autre chat, historique sans date source et boucle sautée après une action confirmée. Un test supplémentaire a constaté que le résultat de conflit ne transmettait pas les autres occupations fixes de la fenêtre. Le test complet de suppression suivie d'une création a ensuite révélé deux reformulations inutiles après une réponse correcte : le filtre rejetait le récit d'une mutation malgré ses reçus vérifiés.

Le contexte du tour possède maintenant un utilisateur, une conversation et un message de référence. Les lectures de demandes en attente, les jugements et les threads d'outils conservent cette portée. Les messages postérieurs à la requête et les autres chats en sont exclus. L'historique porte la date de chaque message. Les jugements existants partagent un instantané borné; ce transport n'ajoute aucun appel de classification.

Les formulaires conservent la question, les libellés et types des champs et leurs valeurs validées côté serveur. Un identifiant de demande source est persisté sur la réponse de l'assistant, y compris lorsqu'une réponse invalide impose de reposer la question. Après un choix valide, cette source est relue dans le même chat et transmise avec le résultat réel de l'outil exécuté par le code. La boucle reprend les actions restantes. Une annulation ne réautorise pas l'action refusée. Les confirmations, cibles, délais de validité, autorisations et protections contre les doublons restent dans l'exécuteur.

Les règles du modèle distinguent les faits utilisateur, les résultats d'outils et les hypothèses. Une correction remplace la valeur concernée; une précision ne change pas implicitement les autres contraintes. Une date relative reste attachée à son message. Une borne connue ne prouve pas une autre borne. Le résultat de conflit indique désormais toutes les fenêtres fixes qui chevauchent la demande et précise que l'obstacle nommé n'est pas une liste exhaustive des éléments à résoudre. Les règles de placement restent inchangées.

Pour les réponses après mutation, la relecture en base précède le contrôle sémantique. Le juge existant compare le texte à la demande, aux résultats et aux reçus. Ce contrôle peut partager son appel avec la vérification d'une question interactive. Un rejet revient au modèle avec ses outils disponibles, dans la limite de deux reprises. Un accord ne donne aucune autorisation d'écriture. Il conserve seulement le texte exact, lié à l'empreinte des preuves; toute modification de ce texte ou des preuves invalide cet accord. Les questions de confirmation gardent priorité.

Si le juge, la relecture ou les preuves complètes ne sont pas disponibles, le rendu revient aux reçus du code, sans payer des reformulations qui ne résoudraient pas cette absence. Les preuves dépassant 16 000 caractères ou un message enrichi dépassant 6 000 caractères ne sont pas soumis comme preuve complète au contrôle de prose. Le jugement sémantique reste probabiliste : ce dispositif ne prouve pas l'absence générale d'hallucination.

Quatre sondes ont réellement appelé le fournisseur Jev depuis l'environnement Railway, sans modifier le planning ni déployer du code. Le résultat du 26 novembre 2026 est :

| Sonde | Verdict attendu et obtenu | Durée |
| --- | --- | --- |
| Effet exact attesté par un reçu | conforme | 86 ms |
| Suppression annoncée sans reçu | contredit | 57 ms |
| Contrainte extérieure inventée | contredit | 59 ms |
| Une partie de la demande abandonnée | incomplet | 60 ms |

Ces quatre observations valident ces exemples, pas une performance générale. La sonde rejouable est `scripts/sonde_validation_recus.py --live`; elle exige une activation explicite, utilise les identifiants configurés et n'écrit aucune donnée métier. Elle ne journalise pas les secrets.

Les tests d'intégration utilisent le vrai graphe PydanticAI, les outils et une base SQLite de test avec un fournisseur scripté. Ils vérifient la confirmation suivie d'une deuxième action, l'absence de doublon, le retour d'un rejet au modèle puis une lecture d'outil, le rendu inchangé de la réponse acceptée, le repli sans juge et la fin bornée des rejets persistants. Les tests unitaires couvrent aussi l'isolation, les dates, les formulaires et l'invalidation d'une approbation devenue périmée.

Commandes locales, avec le Python du projet, `DEBUG=True` et `DATABASE_URL` vide :

```powershell
python manage.py test core.test_agent_v2_continuite core.test_agent_v2_validation_recus core.test_agent_v2_reponse_finale core.test_agent_v2_reprise --noinput -v 1
python manage.py makemigrations --check --dry-run
python manage.py test --parallel 4 --noinput -v 1
$testsFonctions = @(rg -l '^(async )?def test_' . -g 'test*.py' -g '*_test.py' -g 'tests.py')
python -m pytest -q $testsFonctions
```

Validation exécutée : 2 174 tests Django réussis sur le code final, avec code de sortie 0; 148 tests des modules comportant des fonctions pytest réussis; 84 tests ciblés de continuité, confirmations et formulaires réussis. Aucun changement de migration détecté. Les premiers passages complets ont aussi révélé des attentes devenues obsolètes : historique sans date, arrêt après confirmation, instruction de sortie structurée inutilisée et autorisation de deviner une contrainte. Ces attentes ont été corrigées sans supprimer les tests. Le passage pytest complet initial avait 7 échecs; les 152 tests des modules concernés ont ensuite tous réussi sous pytest. Ce passage complet initial n'est pas présenté comme un succès.

Le contexte de conversation accompagne les jugements existants. Le nouveau contrôle sémantique de prose porte sur les réponses finales après mutation vérifiée; il ne constitue pas une validation générale de toutes les questions ou de chaque décision avant exécution. La mesure réelle de 57 à 86 ms concerne les quatre sondes du juge, pas le temps de réponse du chat.

Le gain de latence du chat complet en production et la qualité sur un ensemble représentatif de conversations réelles restent à mesurer après déploiement autorisé. Les compteurs fournisseur historiquement absents, documentés dans le diagnostic de latence, ne sont pas expliqués par ce lot. Aucun déploiement effectué.
