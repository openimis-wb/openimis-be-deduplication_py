from typing import List, Tuple, Type, Union

from django.core.exceptions import FieldDoesNotExist
from core.models import ExtendableModel, HistoryModel


def resolve_columns(
    model: Union[Type[ExtendableModel], Type[HistoryModel]], columns: List[str]
) -> Tuple[List[str], List[str]]:
    """Split column names into real model fields and json_ext keys."""
    fields = []
    json_fields = []
    for column in columns:
        if is_model_column(model, column.split('__', 1)[0]):
            fields.append(column)
        else:
            json_fields.append(column)

    return fields, json_fields


def is_model_column(model: Union[Type[ExtendableModel], Type[HistoryModel]], column: str) -> bool:
    try:
        model._meta.get_field(column)
        return True
    except FieldDoesNotExist:
        return False
