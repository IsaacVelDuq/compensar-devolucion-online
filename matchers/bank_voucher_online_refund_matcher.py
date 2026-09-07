import pandas as pd

from loaders._normalization import normalize_identifier, normalize_money
from core.configuration import CONFIG
from sap.sap_config import REPORT_COLUMNS


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

        tolerance = CONFIG.rules.bank_voucher_tolerance

        refunds = self.online_refunds_df.copy()
        bank = self.bank_voucher_df[
            [CONFIG.inputs.bank_document, CONFIG.inputs.bank_amount]
        ].copy()
        refunds["__documento"] = normalize_identifier(
            refunds[CONFIG.inputs.online_customer_id]
        )
        refunds["__valor"] = normalize_money(
            refunds[CONFIG.inputs.online_amount]
        )
        bank["__documento"] = normalize_identifier(
            bank[CONFIG.inputs.bank_document]
        )
        bank["__valor"] = normalize_money(bank[CONFIG.inputs.bank_amount])

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
        if CONFIG.inputs.online_accounting_document not in df.columns:
            raise ValueError(
                "Faltan columnas requeridas en el DataFrame: "
                f"{CONFIG.inputs.online_accounting_document}"
                        )
        df[CONFIG.inputs.online_accounting_document] = (
            df[CONFIG.inputs.online_accounting_document]
            .str.split(r"\s+")
        )

        return df.explode(
            "Doc contable",
            ignore_index=True,
        )[CONFIG.inputs.online_accounting_document].tolist()

    def get_total_amount(self,df):
        return df[CONFIG.inputs.online_amount].sum()