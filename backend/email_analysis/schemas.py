from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Etat = Literal['a_preparer', 'envoyee', 'entrevue', 'offre_recue', 'offre_acceptee', 'offre_declinee', 'refusee', 'retiree']


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pertinent: bool = Field(strict=True)
    action: Literal['aucune', 'creation', 'changement_etat', 'confirmation_reception']
    employeur: str | None = Field(max_length=200)
    poste: str | None = Field(max_length=200)
    date_candidature: date | None
    etat: Etat | None
    confirmation_reception: bool | None = Field(default=None, strict=True)
    justification: str = Field(max_length=1000)
    informations_manquantes: list[Literal['employeur', 'poste', 'date_candidature', 'etat']] = Field(max_length=4)

    @field_validator('employeur', 'poste')
    @classmethod
    def nonempty_or_null(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                return None
        return value


class DecisionData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['aucune', 'creation', 'changement_etat', 'confirmation_reception']
    employeur: str | None = Field(default=None, max_length=200)
    poste: str | None = Field(default=None, max_length=200)
    date_candidature: date | None = None
    etat: Etat | None = None
    confirmation_reception: bool | None = Field(default=None, strict=True)
    cible_id: int | None = Field(default=None, gt=0, strict=True)

    @field_validator('employeur', 'poste')
    @classmethod
    def nonempty_or_null(cls, value):
        return value.strip() or None if value is not None else None
