from pathlib import Path
import pandas as pd
from datetime import date as date_type

from ._normalization import normalize_date, normalize_identifier, normalize_money
from sap.sap_config import REPORT_COLUMNS


class Fagll03Loader:

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> pd.DataFrame:
        """Carga el archivo descargado de la transacción FAGLL03."""
        try:
            df = pd.read_excel(self.file_path)
            df.columns = df.columns.str.strip()
            return df
        except Exception as error:
            raise ValueError(
                f"Error al cargar el archivo {self.file_path}: {error}"
            ) from error

    def _validate_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Valida las columnas que necesita el matcher FAGLL03."""
        required_columns = [
            REPORT_COLUMNS.text,
            REPORT_COLUMNS.document_number,
            REPORT_COLUMNS.local_amount,
            REPORT_COLUMNS.document_date,
        ]
        missing_columns = [
            column for column in required_columns if column not in df.columns
        ]

        if missing_columns:
            raise ValueError(
                f"Faltan columnas requeridas en el DataFrame: {missing_columns}"
            )
        return df

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normaliza la identificacion, el Nº documento y el importe para el matcher."""
        df = df.copy()

        df["identificacion_cliente"] = (
            df[REPORT_COLUMNS.text]
            .astype("string")
            .str.split()
            .str[0]
        )

        df[REPORT_COLUMNS.document_number] = normalize_identifier(
            df[REPORT_COLUMNS.document_number]
        )
        df["identificacion_cliente"] = normalize_identifier(
            df["identificacion_cliente"]
        )
        df[REPORT_COLUMNS.local_amount] = normalize_money(
            df[REPORT_COLUMNS.local_amount]
        )
        df[REPORT_COLUMNS.document_date] = normalize_date(
            df[REPORT_COLUMNS.document_date]
        )
        return df

    def _filter_dates(self,df:pd.DataFrame,date: date_type):
        cutoff = pd.Timestamp(date).normalize()
        mask = df[REPORT_COLUMNS.document_date] >= cutoff
        return df[mask]

    def process(self, date: date_type | None = None) -> pd.DataFrame:
        """Carga, valida, normaliza y filtra el reporte FAGLL03."""
        df = self.load()
        df = self._validate_columns(df)
        df = self._normalize_df(df)
        if date is not None:
            df = self._filter_dates(df, date)
        return df

    def cargar(self, date: date_type | None = None) -> pd.DataFrame:
        """Alias de compatibilidad para el procesador anterior."""
        if date is None:
            return self.load_and_normalize()
        return self.process(date)

    def load_and_normalize(self) -> pd.DataFrame:
        df = self.load()
        return self._normalize_df(self._validate_columns(df))


Fagll03_loader = Fagll03Loader
Fagll03 = Fagll03Loader




