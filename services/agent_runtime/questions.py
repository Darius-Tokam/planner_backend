"""Typed human input. Validation checks structure, never guesses French intent."""
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .store import InvalidAnswer


class Option(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)


class QuestionField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    type: Literal['text', 'time', 'time_range', 'date', 'datetime', 'number', 'radio', 'select', 'checkbox']
    label: str = Field(min_length=1, max_length=160)
    required: bool = True
    options: list[Option] = Field(default_factory=list, max_length=12)
    allow_other: bool = False

    @model_validator(mode='after')
    def check_options(self):
        if self.type in ('radio', 'select', 'checkbox'):
            if len(self.options) < 2 or len({o.value for o in self.options}) != len(self.options):
                raise ValueError('Au moins deux options distinctes sont nécessaires.')
            if self.type != 'checkbox' and len(self.options) > 4:
                raise ValueError('Au plus quatre choix; pose une question de tri.')
        elif self.options or self.allow_other:
            raise ValueError('Les options appartiennent aux champs à choix.')
        return self


class Choice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['choice', 'text', 'form']
    question: str = Field(min_length=1, max_length=400)
    options: list[Choice] = Field(default_factory=list, max_length=4)
    fields: list[QuestionField] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def check_kind(self):
        if self.kind == 'choice':
            if len(self.options) < 2 or self.fields or len({o.id for o in self.options}) != len(self.options):
                raise ValueError('Deux à quatre choix distincts, sans champs.')
        elif self.options:
            raise ValueError('Options globales réservées au type choice.')
        elif self.kind == 'text' and self.fields:
            raise ValueError('Utilise form pour plusieurs champs.')
        elif self.kind == 'form' and (not self.fields or len({f.id for f in self.fields}) != len(self.fields)):
            raise ValueError('Un à six champs avec des identifiants distincts.')
        return self


def validate_form_answer(question, value):
    if set(value) != {'fields'} or not isinstance(value['fields'], dict):
        raise InvalidAnswer('Réponds aux champs du formulaire.')
    fields = {f['id']: f for f in question['fields']}
    if set(value['fields']) - fields.keys():
        raise InvalidAnswer('Champ inconnu.')
    for key, field in fields.items():
        item = value['fields'].get(key)
        if item in (None, '', []):
            if field.get('required', True):
                raise InvalidAnswer(f"Le champ {field['label']} est requis.")
            continue
        kind = field['type']
        try:
            if kind in ('radio', 'select', 'checkbox'):
                choices = {o['value'] for o in field['options']}
                if isinstance(item, dict):
                    assert field.get('allow_other') and set(item) == {'other'}
                    assert isinstance(item['other'], str) and 0 < len(item['other'].strip()) <= 1000
                elif kind == 'checkbox':
                    assert isinstance(item, list) and len(item) == len(set(item)) and set(item) <= choices
                else:
                    assert isinstance(item, str) and item in choices
            elif kind == 'time_range':
                assert isinstance(item, dict) and set(item) == {'start', 'end'}
                time.fromisoformat(item['start'])
                time.fromisoformat(item['end'])
            elif kind == 'number':
                assert type(item) in (int, float) and abs(item) < 1e12
            else:
                assert isinstance(item, str) and 0 < len(item.strip()) <= 4000
                if kind == 'time':
                    time.fromisoformat(item)
                elif kind == 'date':
                    date.fromisoformat(item)
                elif kind == 'datetime':
                    datetime.fromisoformat(item)
        except (ValueError, TypeError, AssertionError, KeyError):
            raise InvalidAnswer(f"Vérifie le champ {field['label']}.") from None
