import pandas as pd
import re
from pathlib import Path

from core.params import included_bank_payments
from ._normalization import normalize_date, normalize_identifier, normalize_money


class BankVoucherLoader:

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> pd.DataFrame:
        """
        Carga el archivo de comprobante de banco y devuelve un DataFrame.
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
        required_columns = [
            "Documento Beneficiario",
            "Descripción código resultado",
            "Valor",
            "Fecha de transmisión",
        ]

        missing_columns = [
            column
            for column in required_columns
            if column not in df.columns
        ]

        if missing_columns:
            raise ValueError(
                f"Faltan columnas requeridas en el DataFrame: {missing_columns}"
            )
        return df[required_columns]

    def _filter_excluded_payments(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Filtra los pagos excluidos según la lista de pagos excluidos.
        """
        if "Descripción código resultado" not in df.columns:
            raise ValueError(
                "La columna 'Descripción código resultado' "
                "no se encuentra en el DataFrame."
            )

        pattern = "|".join(map(re.escape, included_bank_payments))

        filtered_df = df[
            df["Descripción código resultado"].str.contains(
                pattern, case=False, na=False, regex=True
            )
        ]

        return filtered_df

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normaliza el DataFrame para asegurar consistencia
        en los datos de las columnas.
        """
        df["Documento Beneficiario"] = normalize_identifier(
            df["Documento Beneficiario"]
        )
        df["Descripción código resultado"] = df[
            "Descripción código resultado"
        ].astype("string").str.strip()
        df["Valor"] = normalize_money(df["Valor"])
        df["Fecha de transmisión"] = normalize_date(df["Fecha de transmisión"])
        df = df[~df["Documento Beneficiario"].isna()]


        return df

    def process(self) -> pd.DataFrame:
        """
        Carga, valida, filtra y normaliza el DataFrame
        del comprobante de banco.
        """
        df = self.load()
        df = self._validate_columns(df)
        df = self._filter_excluded_payments(df)
        df = self._normalize_df(df)

        return df

    def get_payment_date(self, df):
        # La fecha de pago es la misma en todos los registros del archivo
        return df["Fecha de transmisión"].dropna().iloc[0]