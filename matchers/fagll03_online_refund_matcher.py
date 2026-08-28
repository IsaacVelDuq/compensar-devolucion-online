import pandas as pd

from loaders._normalization import normalize_date, normalize_identifier, normalize_money


class Fagll03OnlineRefundMatcher:
    """Cruza un reporte FAGLL03 con un reporte de devoluciones online,
    ambos ya cargados en memoria. No conoce archivos ni carpetas."""

    COLUMNAS_FAGLL03 = {
        "identificacion": "identificacion_cliente",
        "valor": "Importe en moneda local",
    }
    COLUMNAS_DEVOLUCIONES = {
        "identificacion": "Cédula cliente o nit",
        "valor": "Valor devolución o saldo",
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
            fagll03[["__identificacion", "__valor", "identificacion_cliente", "Nº documento"]],
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
        tolerance: float = 1_000,
    ) -> pd.DataFrame:
        """Encuentra el pago negativo más cercano por valor y fecha."""
        candidates = df_fagll03.copy()
        candidates["__amount"] = normalize_money(
            candidates["Importe en moneda local"]
        )
        candidates["__amount_diff"] = (
            candidates["__amount"] + abs(amount)
        ).abs()
        candidates["__date"] = normalize_date(candidates["Fecha de documento"])
        payment_date = pd.Timestamp(payment_date).normalize()
        candidates["__date_diff"] = (candidates["__date"] - payment_date).abs()
        candidates = candidates[
            (candidates["__amount"] < 0)
            & (candidates["__amount_diff"] <= tolerance)
        ].sort_values(["__amount_diff", "__date_diff"], kind="stable")
        return candidates.head(1).drop(
            columns=["__amount", "__amount_diff", "__date", "__date_diff"]
        )