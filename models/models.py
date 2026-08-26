


from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


@dataclass(frozen=True)
class AccountClearingItem:
    """
    Representa una cuenta a compensar junto con las partidas (documentos)
    abiertas que le corresponden en F-53.
    """
    account: str
    docs: list[str]

    @classmethod
    def from_dataframe(
        cls,
        df: "pd.DataFrame",
        account_col: str = "cuenta",
        doc_col: str = "documento",
    ) -> list["AccountClearingItem"]:
        """
        Construye la lista de AccountClearingItem a partir de un DataFrame
        donde cada FILA es un documento individual (una cuenta puede
        repetirse en varias filas, una por cada documento asociado).

        Ejemplo de entrada:

            cuenta      documento
            0001234567  1900000123
            0001234567  1900000124
            0009876543  1900000200

        Resultado:

            [
                AccountClearingItem(account="0001234567", docs=["1900000123", "1900000124"]),
                AccountClearingItem(account="0009876543", docs=["1900000200"]),
            ]
        """
        if account_col not in df.columns:
            raise ValueError(f"La columna '{account_col}' no existe en el DataFrame.")

        if doc_col not in df.columns:
            raise ValueError(f"La columna '{doc_col}' no existe en el DataFrame.")

        items: list[AccountClearingItem] = []

        grouped = df.groupby(account_col)[doc_col].apply(list)

        for account, docs in grouped.items():
            items.append(
                cls(
                    account=str(account),
                    docs=[str(doc) for doc in docs],
                )
            )

        return items