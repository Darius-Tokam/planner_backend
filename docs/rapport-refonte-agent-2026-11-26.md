# Refonte du moteur Planner : résultat et preuves

26 novembre 2026. Implémentation sur `codex/refonte-agent-durable`, backend et frontend isolés. Aucun merge ni déploiement de cette refonte. Le moteur v2 reste le défaut de production.

## Résultat

Le nouveau moteur utilise une boucle PydanticAI, avec reprise persistée par DBOS et état métier PostgreSQL. Le texte public du modèle est transmis progressivement. Les cartes d’action affichent les reçus contrôlés par le serveur. Aucun juge ne retient systématiquement la prose avant sa publication.

La compréhension de la demande reste au modèle. Le code vérifie les paramètres, le propriétaire, le consentement, la portée, l’expiration, les cibles et les effets. Une erreur d’outil retourne au modèle pour correction ou explication. Il n’y a pas de nouvelle liste de mots destinée à classifier les demandes.

Une question conserve son identifiant d’appel, son formulaire et sa réponse. Le serveur reprend cette interaction avec son protocole original. Une réponse rapide, reçue avant la fin de l’invocation, rejoint le même mécanisme de reprise. Un refus ou une cible modifiée interdit la mutation préparée.

L’extraction documentaire est une lecture. Le modèle reçoit les données ou l’erreur; `import_document` effectue ensuite un import explicitement demandé. Les permissions sont revérifiées au commit. Les résultats volumineux et les demandes anciennes disposent de références de relecture bornée.

Les tentatives fournisseur et leurs réservations de budget sont enregistrées atomiquement. La mesure, le checkpoint de réponse et la clôture de réservation partagent également une transaction. Un replay d’une réponse sauvegardée ne facture pas un nouvel appel. Une tentative sans mesure reste distincte du quota utilisateur mesuré.

## Mesures avec DeepSeek réel

Modèle `deepseek-flash`, raisonnement désactivé, concurrence 1, comptes synthétiques PostgreSQL locaux. Les fichiers contiennent les réponses publiques et les mesures, jamais les clés ni le raisonnement interne.

- [Corpus final de 44 cas](evaluations/2026-11-26/runtime-deepseek-44.json) : **43/44 selon l’oracle automatique**, dont 8/8 paraphrases réservées. Médiane 2,77 s, percentile 95 à 4,25 s. Ces durées incluent les outils et l’état terminal de l’invocation, y compris l’attente d’une réponse humaine.
- Le cas `invalid_id` est classé en échec parce que l’oracle attend une opération de lecture enregistrée. Le protocole montre bien `list_tasks`, puis `target_not_found`; la seule tâche fixture « Rapport » reste inchangée et aucune mutation n’est enregistrée. La réponse explique correctement l’absence. L’oracle n’a pas été réécrit après ce résultat pour afficher 44/44.
- Les deux cas de date limite absente ont été relus manuellement : le modèle indique qu’aucune date n’est enregistrée. L’oracle automatique de ces cas ne prouve pas à lui seul la vérité de la prose.
- Le formulaire d’examen final ne bloque que sur les heures, mais propose encore deux champs facultatifs et les présente trop fortement dans sa question. Une [campagne précédente](evaluations/2026-11-26/runtime-before-review-44.json) imposait parfois un titre inutile : **42/44**. Cela reste une limite de qualité du modèle à suivre pendant la cohorte.

Comparaison sur les huit mêmes demandes, avec [l’ancien moteur local](evaluations/2026-11-26/legacy-matched-8.json), le même modèle et le même réglage de raisonnement :

| Mesure | Ancien moteur | Nouveau moteur | Écart |
| --- | ---: | ---: | ---: |
| Durée médiane, 8 demandes | 5,97 s | 2,65 s | -55,6 % |
| Premier texte médian, 7 demandes comparables | 4,42 s | 2,29 s | -48,3 % |
| Jetons mesurés cumulés, 8 demandes | 306 782 | 136 696 | -55,4 % |

La confirmation de suppression n’a pas de premier texte dans le nouveau moteur : elle affiche une interaction, donc ce cas est exclu de la comparaison du premier texte. Une demande de tâche sans titre passe de 1,94 à 3,53 s : le gain n’est pas uniforme. Le cache fournisseur représente 401 280 des 768 181 jetons d’entrée de la campagne finale, soit 52,2 %.

La référence ancienne désactive Jev et les fournisseurs de repli. Ce sont des mesures locales reproductibles, pas une comparaison A/B de production. Les échantillons sont petits, le cache et la latence réseau varient. Aucun chiffre ne garantit le comportement d’un prochain appel.

Une tentative intermédiaire a échoué sur les 44 cas avant accès au modèle : le réseau substituait un certificat TLS non reconnu. Elle n’est pas utilisée pour calculer les gains. Darius a changé de réseau, la vérification TLS a réussi, puis la campagne complète a été relancée. TLS n’a pas été désactivé pour transmettre une clé.

## Vérifications exécutées

| Vérification | Résultat |
| --- | --- |
| Suite Django complète, SQLite | 2 238 tests exécutés, succès, 4 tests PostgreSQL ignorés sur SQLite |
| Suite pytest complète, SQLite | 2 382 réussis, 4 ignorés, 359 avertissements |
| Runtime avec PostgreSQL 16.16 réel | 64/64, dont concurrence et corrections de revue |
| Frontend Vitest | 383/383 |
| TypeScript et build frontend | Succès |
| Migration 0051, PostgreSQL | Appliquée avec succès |
| Crash après commit métier avant acquittement DBOS | Processus tué puis repris, un effet, un reçu |
| Adaptateur DBOSAgent réel | Outil, reçu, texte progressif, replay sans nouvelle facturation |
| Suppression de compte | Purge réelle du workflow DBOS et de ses enfants SDK |
| Navigateur Edge, 390 × 844 et 1280 × 900 | Question après rechargement, réponse 15:00 à 17:00, reçu, état terminal, nouveau message dans le même chat, aucun débordement horizontal ni erreur JavaScript |

Les suites Django et pytest se recouvrent; leurs nombres ne s’additionnent pas. Les quatre tests ignorés sur SQLite passent dans la suite PostgreSQL. Les preuves de crash et de DBOS utilisent un fournisseur contrôlé. Le [parcours navigateur final](evaluations/2026-11-26/browser-real.json) utilise DeepSeek réel; une exécution séparée avec fournisseur contrôlé a également réussi.

La première tentative navigateur contrôlée utilisait un compte ayant déjà un examen au même horaire. L’outil a refusé le conflit. Les essais finaux utilisent des comptes synthétiques distincts pour ne pas confondre des fixtures accumulées avec une panne de transport.

## Revue indépendante et corrections

Une revue indépendante a examiné la branche complète. Ses problèmes critiques et importants ont été traités dans une seule passe, avec reproductions négatives puis tests verts :

1. Compte figé pendant l’admission, contrôles après les attentes réseau et avant les écritures, callbacks bornés à la conversation. Un refresh tardif de l’ancien compte ne déconnecte plus le nouveau.
2. Convergence atomique entre réponse humaine rapide et transition d’attente du worker.
3. Empreinte des cibles et dépendances avant confirmation, y compris créneaux supprimés en cascade; date d’optimisation figée.
4. Extraction sans import automatique et nouvelle vérification du consentement avant persistance.
5. File de purge indépendante des suppressions en cascade, annulation et suppression des workflows et enfants, réessai conservé en cas d’erreur.
6. Identité d’appel contrôlée entre interactions et opérations, avec vérification du reçu lors de la reprise.
7. Événement de nouvelle tentative retirant les fragments interrompus, même si la nouvelle tentative produit seulement des outils.
8. Admission et règlement du budget atomiques avec le checkpoint fournisseur.
9. Historique récent au-delà de 200 messages, conversation adoptée sans effacer la file, récupération v3 après rechargement.
10. Suivi des interactions en attente maintenu pour recevoir réponse depuis un autre client et expiration.
11. Références invalides renvoyées comme erreurs d’outil, résultats paginés et références aux demandes omises du contexte.

Point mineur différé : renforcer l’oracle sémantique des réponses, notamment `null_deadline`. Le taux automatique publié porte sur les contrats vérifiés par le corpus, pas sur toute la vérité de la prose.

## Activation et limites

Voir le [guide d’exploitation](runtime-v3-operations.md) pour migration, worker, cohorte, rollback et commandes. L’ancien chat reste sur v2. Aucun compte réel n’a été migré par ces tests.

La livraison d’une notification sur iPhone, TestFlight, APNs, plusieurs réplicas et les conditions de production n’ont pas été validés par cette refonte. Les garde-fous métier existants et le canal de notification restent utilisés. La qualité des extractions avec un fournisseur documentaire réel n’est pas démontrée par les tests contrôlés du parseur.

La purge exige un worker actif; les erreurs de sa file doivent être surveillées. Une durée de rétention pour les comptes actifs reste à définir avant ouverture générale. La prose en direct conserve le risque d’erreur accepté lors du choix de l’architecture. Les reçus contrôlés demeurent la preuve des actions.

Les décisions d’implémentation, erreurs rencontrées et coûts des arbitrages sont conservés dans le registre de livraison associé. Les dépôts de travail originaux n’ont pas été modifiés.
