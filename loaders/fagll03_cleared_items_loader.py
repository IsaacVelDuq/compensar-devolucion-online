from pathlib import Path

import pandas as pd

from ._normalization import normalize_identifier, normalize_money, normalize_date
from sap.sap_config import REPORT_COLUMNS


class Fagll03ClearedItemsLoader:
    """
    Carga y normaliza el Excel de "partidas compensadas" descargado por
    `FAGLL03ClearedItemsReportGenerator`, dejándolo listo para el cruce
    contra `pending` en `Fagll03ClearedItemsMatcher`.
    """

    REQUIRED_COLUMNS = [
        REPORT_COLUMNS.document_number,
        REPORT_COLUMNS.local_amount,
        REPORT_COLUMNS.text,
        REPORT_COLUMNS.clearing_document,
        REPORT_COLUMNS.clearing_date,
        REPORT_COLUMNS.document_date,
    ]

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> pd.DataFrame:
        """
        Carga el archivo de partidas compensadas y devuelve un DataFrame.
        """
        try:
            df = pd.read_excel(self.file_path)
            df.columns = df.columns.str.strip()  # Eliminar espacios en los nombres de las columnas
            return df

        except Exception as e:
            raise ValueError(
                f"Error al cargar el archivo {self.file_path}: {e}"
            )

    def _validate_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Valida que el DataFrame tenga las columnas necesarias.
        """
        missing_columns = [
            column
            for column in self.REQUIRED_COLUMNS
            if column not in df.columns
        ]

        if missing_columns:
            raise ValueError(
                f"Faltan columnas requeridas en el DataFrame: {missing_columns}"
            )
        return df[self.REQUIRED_COLUMNS]

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normaliza el DataFrame para asegurar consistencia en los datos
        de las columnas, y deriva la identificación del cliente a partir
        de la columna "Texto" (primer token al dividir por espacios;
        misma regla que usaba el `FAGLL03ClearedItemsMatcher` anterior
        al leer la lista en vivo).
        """
        df = df.copy()

        for column in [
            REPORT_COLUMNS.document_number,
            REPORT_COLUMNS.clearing_document,
            REPORT_COLUMNS.text,
        ]:
            df[column] = df[column].astype("string").str.strip()

        for column in [
            REPORT_COLUMNS.document_number,
            REPORT_COLUMNS.clearing_document,
        ]:
            df[column] = normalize_identifier(df[column])

        for column in [REPORT_COLUMNS.local_amount]:
            df[column] = normalize_money(df[column])

        df["identificacion_texto"] = normalize_identifier(
            df[REPORT_COLUMNS.text].str.split().str[0]
        )

        for column in [REPORT_COLUMNS.clearing_date, REPORT_COLUMNS.document_date]:
            df[column] = normalize_date(df[column])

        df = df[~df["identificacion_texto"].isna()]

        return df

    def process(self) -> pd.DataFrame:
        """
        Carga, valida y normaliza el DataFrame de partidas compensadas.
        """
        df = self.load()
        df = self._validate_columns(df)
        df = self._normalize_df(df)

        return df