"""Sonde opt-in du juge réel : aucun accès en écriture au planning.

Depuis la racine du dépôt : python scripts/sonde_validation_recus.py --live
Utilise les identifiants configurés; n'imprime que verdicts et durées.
"""
import json
import os
from pathlib import Path
import sys
import time


def main():
    if '--live' not in sys.argv:
        raise SystemExit('Ajouter --live pour appeler le fournisseur réel.')
    sys.path.insert(0, str(Path.cwd()))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'planner.settings')
    import django
    django.setup()
    import requests
    from services.agent_v2 import jugement
    from services.agent_v2.validation_recus import question

    if not jugement._cle_jev():
        raise SystemExit('Clé du juge absente; aucune sonde exécutée.')
    preuve = {'outil': 'create_task', 'succes': True, 'mutation': True,
              'etat_verifie': True, 'resultat': {'title': 'Relire le contrat'},
              'recu': {'statut': 'confirme', 'source': 'base', 'postconditions': [
                  {'entite': 'task', 'id': 1, 'attendu': {'title': 'Relire le contrat'},
                   'observe': {'title': 'Relire le contrat'}, 'conforme': True}]}}
    cas = [
        ('effet_exact', 'Crée la tâche Relire le contrat.',
         'La tâche Relire le contrat est créée.', 'conforme'),
        ('effet_non_prouve', 'Crée la tâche Relire le contrat.',
         'Tous tes rendez-vous ont été supprimés.', 'contredit'),
        ('contrainte_inventee', 'Crée la tâche Relire le contrat. Mon bureau ouvre à 8 h.',
         'La tâche est créée. Comme ton bureau ferme à 17 h, tu dois finir avant.', 'contredit'),
        ('demande_abandonnee', 'Crée Relire le contrat et programme un rappel demain à 10 h.',
         'Tout est fait : la tâche est créée.', 'incomplet'),
    ]
    reussis = 0
    for nom, demande, reponse, attendu in cas:
        questions = {'reponse_etayee': question()}
        etat = {'message': demande, 'reponse_assistant': reponse,
                'contexte_conversation': {'maintenant': '2026-11-26T09:00:00-05:00'},
                'resultats_et_recus': [preuve]}
        debut = time.perf_counter()
        try:
            resultat = requests.post(jugement._url_jev(),
                headers={'Authorization': 'Bearer ' + jugement._cle_jev()},
                json={'model': jugement._modele_jev(), 'state': etat,
                      'questions': {cle: jugement._question_jev(q) for cle, q in questions.items()}},
                timeout=5)
            resultat.raise_for_status()
            verdict = jugement._appliquer_seuil(jugement._valider_reponse(resultat.json(), questions))['reponse_etayee']
            ok = verdict['statut'] == 'decision' and verdict['valeur'] == attendu
            reussis += int(ok)
            print(json.dumps({'cas': nom, 'attendu': attendu, 'verdict': verdict,
                'duree_ms': round((time.perf_counter() - debut) * 1000), 'conforme': ok}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'cas': nom, 'erreur': type(exc).__name__, 'conforme': False}))
    raise SystemExit(0 if reussis == len(cas) else 1)


if __name__ == '__main__':
    main()
