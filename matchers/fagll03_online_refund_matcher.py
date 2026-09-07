import pandas as pd

from loaders._normalization import normalize_date, normalize_identifier, normalize_money
from core.configuration import CONFIG
from sap.sap_config import FAGLL03Config, REPORT_COLUMNS


class Fagll03OnlineRefundMatcher:
    """Cruza un reporte FAGLL03 con un reporte de devoluciones online,
    ambos ya cargados en memoria. No conoce archivos ni carpetas."""

    COLUMNAS_FAGLL03 = {
        "identificacion": REPORT_COLUMNS.customer_id,
        "valor": REPORT_COLUMNS.local_amount,
    }
    COLUMNAS_DEVOLUCIONES = {
        "identificacion": CONFIG.inputs.online_customer_id,
        "valor": CONFIG.inputs.online_amount,
    }

    @classmethod
    def match(
        cls,
        df_fagll03: pd.DataFrame,
        df_online_refund: pd.DataFrame,

    ) -> pd.DataFrame:
        """devoluciones online es el lado izquierdo. La columna ``_merge``
        contiene ``both`` cuando existe coincidencia y ``left_only`` cuando
        no existe en el reporte FAGLL03."""
        online = df_online_refund.copy()
        fagll03 = df_fagll03.copy()
        online["__identificacion"] = normalize_identifier(
            online[cls.COLUMNAS_DEVOLUCIONES["identificacion"]]
        )
        fagll03["__identificacion"] = normalize_identifier(
            fagll03[cls.COLUMNAS_FAGLL03["identificacion"]]
        )
        online["__valor"] = normalize_money(
            online[cls.COLUMNAS_DEVOLUCIONES["valor"]]
        ).abs().round(2)
        fagll03["__valor"] = normalize_money(
            fagll03[cls.COLUMNAS_FAGLL03["valor"]]
        ).abs().round(2)

        resultado = online.merge(
            fagll03[
                [
                    "__identificacion",
                    "__valor",
                    REPORT_COLUMNS.customer_id,
                    REPORT_COLUMNS.document_number,
                ]
            ],
            how="left",
            on=["__identificacion", "__valor"],
            indicator=True,
            suffixes=("_online", "_fagll03"),
        )
        return resultado.drop(
            columns=["__identificacion", "__valor"], errors="ignore"
        )

    @classmethod
    def find_payment(
        cls,
        df_fagll03: pd.DataFrame,
        amount: float,
        payment_date: pd.Timestamp,
        tolerance: float = FAGLL03Config().tolerance,
    ) -> pd.DataFrame:
        """Encuentra el pago negativo más cercano por valor y fecha."""
        candidates = df_fagll03.copy()
        candidates["__amount"] = normalize_money(
            candidates[REPORT_COLUMNS.local_amount]
        )
        candidates["__amount_diff"] = (
            candidates["__amount"] + abs(amount)
        ).abs()
        candidates["__date"] = normalize_date(
            candidates[REPORT_COLUMNS.document_date]
        )
        payment_date = pd.Timestamp(payment_date).normalize()
        candidates["__date_diff"] = (candidates["__date"] - payment_date).abs()
        candidates = candidates[
            (candidates["__amount"] < 0)
            & (candidates["__amount_diff"] <= tolerance)
        ].sort_values(["__amount_diff", "__date_diff"], kind="stable")
        return candidates.head(1).drop(
            columns=["__amount", "__amount_diff", "__date", "__date_diff"]
        )