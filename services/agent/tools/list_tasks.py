"""Outil list_tasks."""
from typing import Literal

from django.contrib.auth.models import User
from django.db.models import Q
from pydantic import BaseModel, ConfigDict, Field

from core.models import Task
from . import commun
from .base import ToolResult

NAME = "list_tasks"
DESCRIPTION = (
    "Liste les tâches de l'utilisateur. Peut filtrer par task_id exact, statut, type, texte "
    "ou limite. Pour retrouver une tâche par nom, sujet ou personne, passe "
    "query au lieu de répéter une liste non filtrée. Si truncated=true, lis la suite avec next_offset."
)

# Source de verite : les choix du modele (via commun). Le Literal
# ci-dessous est le miroir type du schema ; l'assertion garde les
# deux synchronises (miroir du pattern PicProductivite de
# update_preferences.py).
TypeTache = Literal["deep_work", "shallow", "errand"]
assert set(TypeTache.__args__) == commun.VALID_TASK_TYPES, (
    "derive entre le schema type et Task.TASK_TYPE_CHOICES"
)


class Parametres(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: int | None = Field(default=None, ge=1, description="Identifiant exact de la tâche à lire, dans ce compte uniquement.")
    offset: int = Field(default=0, ge=0, description="Début de la page; utiliser next_offset pour lire la suite.")

    completed: bool | None = Field(
        default=None,
        description="Filtrer par statut (true=terminées, false=en cours)",
    )
    task_type: TypeTache | None = Field(
        default=None,
        description="Filtrer par type",
    )
    query: str | None = Field(
        default=None, max_length=200,
        description=(
            "Texte à chercher dans le titre, la description ou le cours "
            "lié, par exemple le nom d'une personne."
        ),
    )
    limit: int | None = Field(
        default=None, ge=1, le=50,
        description="Nombre max de tâches à retourner (défaut: 20)",
    )


PARAMETERS = Parametres.model_json_schema()


def execute(user: User, **kwargs) -> ToolResult:
    tasks = Task.objects.filter(user=user).order_by('-created_at', '-pk')
    if kwargs.get('task_id') is not None:
        tasks = tasks.filter(pk=kwargs['task_id'])

    completed = kwargs.get("completed")
    if completed is not None:
        tasks = tasks.filter(completed=completed)

    task_type = kwargs.get("task_type")
    if task_type:
        tasks = tasks.filter(task_type=task_type)

    query = str(kwargs.get("query") or "").strip()
    if query:
        tasks = tasks.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(related_course__icontains=query)
        ).order_by("-created_at", "-pk")

    limit, offset = kwargs.get('limit') or 20, kwargs.get('offset', 0)
    total = tasks.count()
    tasks = tasks[offset:offset + limit]
    task_list = [commun._task_to_dict(t) for t in tasks]

    return ToolResult(
        success=True,
        data={"tasks": task_list, "count": len(task_list), "total": total,
              "truncated": offset + len(task_list) < total,
              "next_offset": offset + len(task_list) if offset + len(task_list) < total else None},
        message=f"{len(task_list)} tâche(s) trouvée(s).",
    )
