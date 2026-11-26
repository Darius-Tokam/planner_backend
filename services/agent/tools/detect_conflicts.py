"""Outil detect_conflicts."""
from datetime import date, timedelta
from django.utils import timezone
from services.scheduling.constraints import day_constraints, describe
from django.contrib.auth.models import User
from pydantic import BaseModel, ConfigDict, Field

from .base import ISSUE_TERMINEE, ToolResult
from . import commun
from .commun import DAY_NAMES

NAME = "detect_conflicts"
DESCRIPTION = "Détecte les conflits du planning sur sept jours à partir de start_date (défaut aujourd'hui), en distinguant activités, transport, préparation et sommeil. Inclut les tâches datées et les occurrences réelles."


class Parametres(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_date: str | None = Field(default=None, description="Début de la fenêtre de sept jours, YYYY-MM-DD; défaut aujourd’hui.")

    day_of_week: commun.JourChaine = Field(
        default=None,
        description="Vérifier un jour spécifique. Nom de préférence (\"samedi\"); les numéros restent acceptés (0=Lundi..6=Dimanche). Si absent, vérifie tous les jours.",
    )


PARAMETERS = Parametres.model_json_schema()


def execute(user: User, **kwargs) -> ToolResult:
    # Nom ou numero, comme partout ailleurs depuis le correctif du
    # decalage +1: une convention par outil serait une invitation a se
    # tromper.
    from .commun import normaliser_jours
    jours = normaliser_jours(kwargs.get("day_of_week"))
    try:
        start = date.fromisoformat(kwargs['start_date']) if kwargs.get('start_date') else timezone.localdate()
    except (TypeError, ValueError):
        return ToolResult(False, message='Date invalide. Utilise YYYY-MM-DD.')
    conflicts = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        if jours and day.weekday() != jours[0]:
            continue
        constraints = day_constraints(user, day, hard_only=True)
        for index, left in enumerate(constraints):
            for right in constraints[index + 1:]:
                identity = lambda c: (c.source['type'], c.source['id'], c.source['date'])
                if identity(left) == identity(right) or not right.overlaps(left.start, left.end):
                    continue
                conflicts.append({
                    'date': day.isoformat(), 'day': day.weekday(), 'day_name': DAY_NAMES[day.weekday()],
                    'block_a': {**left.source, 'time': f"{left.source['start_time']}-{left.source['end_time']}"},
                    'block_b': {**right.source, 'time': f"{right.source['start_time']}-{right.source['end_time']}"},
                    'constraints': [left.as_dict((right.start, right.end)), right.as_dict((left.start, left.end))],
                    'explanation': describe(left) + ' chevauche ' + describe(right),
                    'overlap_minutes': min(left.end, right.end) - max(left.start, right.start),
                })

    # Contrat d'issue (audit 2026-10-13): les titres sont des donnees
    # lues, on les nomme sans rien inventer.
    if conflicts:
        lignes = '\n'.join(f"- {c['date']} : {c['explanation']}" for c in conflicts)
        public = f"{len(conflicts)} conflit(s) dans ton horaire :\n{lignes}"
    else:
        public = "Aucun conflit détecté dans ton horaire."
    return ToolResult(
        success=True,
        data={"conflicts": conflicts, "count": len(conflicts),
              "start_date": start.isoformat(), "end_date": (start + timedelta(days=6)).isoformat()},
        message=f"{len(conflicts)} conflit(s) détecté(s)." if conflicts else "Aucun conflit détecté.",
        issue=ISSUE_TERMINEE, cause="", public=public,
    )
