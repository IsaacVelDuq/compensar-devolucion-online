import pandas as pd

from pathlib import Path


class FBL5NLoader:

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> pd.DataFrame:
        """
        Carga el archivo descargado de la transacción FBL5N y devuelve un DataFrame.
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
            "Cuenta",
            "Nº documento",
            "Importe en moneda local",
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
        for column in [
            "Cuenta",
            "Nº documento",
        ]:
            df[column] = df[column].astype("string").str.strip()

        for column in ["Importe en moneda local"]:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        #Se debe procesar unicamente los registros que tengan datos reales en cuenta y n documento
        for column in [
            "Cuenta",
            "Nº documento",
        ]:
            df[column] = (
                df[column]
                .replace(r"^\s*$", pd.NA, regex=True)
                .astype("string")
                .str.replace(r"\.0$", "", regex=True)
            )

        df = df.dropna(subset=["Nº documento", "Cuenta"])

        df = df.dropna(subset=["Nº documento","Cuenta"])
        return df


    def get_total_amount(self,df):
        return df["Importe en moneda local"].sum() * -1


    def get_docs(self,df):
        return df["Nº documento"].tolist()
    
    def process(self) -> pd.DataFrame:
        """
        Carga, valida, normaliza y separa los documentos
        del DataFrame de devoluciones online.
        """
        df = self.load()
        df = self._validate_columns(df)
        df = self._normalize_df(df)
        return df

    