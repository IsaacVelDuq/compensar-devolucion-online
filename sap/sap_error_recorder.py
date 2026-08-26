from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd


ERROR_COLUMNS = (
    "fecha",
    "modulo",
    "operacion",
    "tipo_error",
    "mensaje",
)


def record_error(
    df_errores: pd.DataFrame | None,
    modulo: str,
    operacion: str,
    error: BaseException,
) -> None:
    """Agrega un error visible para el usuario sin reemplazar el DataFrame."""
    if df_errores is None:
        return

    for column in ERROR_COLUMNS:
        if column not in df_errores.columns:
            df_errores[column] = pd.Series(dtype="object")

    df_errores.loc[len(df_errores)] = {
        "fecha": datetime.now(),
        "modulo": modulo,
        "operacion": operacion,
        "tipo_error": type(error).__name__,
        "mensaje": str(error),
    }


def ensure_error_columns(df_errores: pd.DataFrame | None) -> pd.DataFrame:
    """Inicializa las columnas públicas del acumulador de errores."""
    if df_errores is None:
        return pd.DataFrame(columns=ERROR_COLUMNS)

    for column in ERROR_COLUMNS:
        if column not in df_errores.columns:
            df_errores[column] = pd.Series(dtype="object")
    return df_errores
