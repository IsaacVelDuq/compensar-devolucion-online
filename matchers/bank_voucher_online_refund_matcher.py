import pandas as pd

from loaders._normalization import normalize_identifier, normalize_money


class BankVoucherOnlineRefundMatcher:

    def __init__(
        self,
        bank_voucher_df: pd.DataFrame,
        online_refunds_df: pd.DataFrame,
    ):
        self.bank_voucher_df = bank_voucher_df
        self.online_refunds_df = online_refunds_df

    def match(self) -> pd.DataFrame:
        """
        Cruza las devoluciones online contra los pagos realizados en banco.

        Reglas:
        - El documento debe coincidir.
        - El valor puede variar dentro de una tolerancia.
        - Cada registro del banco solo puede utilizarse una vez.
        - Evita generar duplicados cuando existen documentos y valores repetidos.
        """

        tolerance = 10.0

        refunds = self.online_refunds_df.copy()
        bank = self.bank_voucher_df[["Documento Beneficiario", "Valor"]].copy()
        refunds["__documento"] = normalize_identifier(
            refunds["Cédula cliente o nit"]
        )
        refunds["__valor"] = normalize_money(refunds["Valor devolución o saldo"])
        bank["__documento"] = normalize_identifier(bank["Documento Beneficiario"])
        bank["__valor"] = normalize_money(bank["Valor"])

        # Identificadores para saber qué registro del banco
        # ya fue utilizado
        bank["_bank_id"] = range(len(bank))

        matched_rows = []
        used_bank_ids = set()

        for refund_index, refund in refunds.iterrows():

            documento = refund["__documento"]
            valor_refund = refund["__valor"]

            candidatos = bank[
                (bank["__documento"] == documento)
                & (
                    (bank["__valor"] - valor_refund).abs() <= tolerance
                )
                & (~bank["_bank_id"].isin(used_bank_ids))
            ].copy()

            if candidatos.empty:
                continue

            # Elegir el pago más cercano al valor de la devolución
            candidatos["_diferencia"] = (
                candidatos["__valor"] - valor_refund
            ).abs()

            candidato = candidatos.sort_values(
                "_diferencia"
            ).iloc[0]

            used_bank_ids.add(candidato["_bank_id"])

            matched_rows.append(refund_index)

        return refunds.loc[matched_rows].drop(
            columns=["__documento", "__valor"], errors="ignore"
        ).reset_index(drop=True)
    
    def split_docs(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Divide los valores de la columna "Doc contable" en múltiples filas
        cuando contienen más de un documento separado por espacios.
        """
        df = df.copy()
        if "Doc contable" not in df.columns:
            raise ValueError(
                f"Faltan columnas requeridas en el DataFrame: {'Doc contable'}"
                        )
        df["Doc contable"] = (
            df["Doc contable"]
            .str.split(r"\s+")
        )

        return df.explode(
            "Doc contable",
            ignore_index=True,
        )["Doc contable"].tolist()

    def get_total_amount(self,df):
        return df["Valor devolución o saldo"].sum()