import pandas as pd

from pathlib import Path
from ._normalization import normalize_identifier, normalize_money


class OnlineRefundsLoader:

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> pd.DataFrame:
        """
        Carga el archivo de devoluciones online y devuelve un DataFrame.
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
            "Cédula cliente o nit",
            "Valor devolución o saldo",
            "Doc contable",
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

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normaliza el DataFrame para asegurar consistencia
        en los datos de las columnas.
        """
        df["Cédula cliente o nit"] = normalize_identifier(
            df["Cédula cliente o nit"]
        )
        df["Doc contable"] = normalize_identifier(df["Doc contable"])
        df["Valor devolución o saldo"] = normalize_money(
            df["Valor devolución o saldo"]
        )
        df = df[~df["Cédula cliente o nit"].isna()]

        return df

    def process(self) -> pd.DataFrame:
        """
        Carga, valida, normaliza y separa los documentos
        del DataFrame de devoluciones online.
        """
        df = self.load()
        df = self._validate_columns(df)
        df = self._normalize_df(df)
        return df

    def cargar(self) -> pd.DataFrame:
        return self.process()


DevolucionesOnline = OnlineRefundsLoader