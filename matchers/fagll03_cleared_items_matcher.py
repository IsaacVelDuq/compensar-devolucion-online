import pandas as pd

from loaders._normalization import normalize_identifier, normalize_money


class Fagll03ClearedItemsMatcher:
    """
    Cruza el reporte de "partidas compensadas" (ya cargado y normalizado
    por `Fagll03ClearedItemsLoader`) contra `pending`.
    """

    @classmethod
    def match(
        cls,
        df_cleared: pd.DataFrame,
        pending: pd.DataFrame,
        id_column: str = "Cédula cliente o nit",
        value_column: str = "Valor devolución o saldo",
        tolerance: float = 1_000,
    ) -> pd.DataFrame:

        cleared = df_cleared.copy().reset_index(drop=True)
        result = pending.copy().reset_index(drop=True)

        result["__identificacion_match"] = normalize_identifier(
            result[id_column]
        )

        result["__valor_match"] = (
            normalize_money(result[value_column])
            .abs()
            .round(2)
        )

        result["Nº documento"] = pd.NA
        result["Doc.compensación"] = pd.NA
        result["Importe en moneda local"] = pd.NA
        result["identificacion_coincide"] = False

        result["Fecha compensación"] = pd.Series(
            pd.NaT,
            index=result.index,
            dtype="datetime64[ns]",
        )

        used_indexes = set()

        for idx, row in result.iterrows():

            expected_id = row["__identificacion_match"]
            expected_value = row["__valor_match"]

            if pd.isna(expected_id) or pd.isna(expected_value):
                continue

            candidates = cleared[
                ~cleared.index.isin(used_indexes)
            ].copy()

            candidates["__diff"] = (
                candidates["Importe en moneda local"].abs()
                - expected_value
            ).abs()

            candidates = candidates[
                candidates["__diff"] <= tolerance
            ]

            if candidates.empty:
                continue

            match = candidates[
                candidates["identificacion_texto"] == expected_id
            ]

            if match.empty:
                continue

            # Menor diferencia de importe.
            # En caso de empate, documento más reciente.
            match = match.sort_values(
                ["__diff", "Fecha de documento"],
                ascending=[True, False],
                kind="stable",
            )

            selected_index = match.index[0]
            selected = cleared.loc[selected_index]

            used_indexes.add(selected_index)

            result.at[idx, "Nº documento"] = selected["Nº documento"]
            result.at[idx, "Doc.compensación"] = selected["Doc.compensación"]
            result.at[idx, "Importe en moneda local"] = (
                selected["Importe en moneda local"]
            )
            result.at[idx, "identificacion_coincide"] = True
            result.at[idx, "Fecha compensación"] = (
                selected["Fecha compensación"]
            )

        return result.drop(
            columns=[
                "__identificacion_match",
                "__valor_match",
            ],
            errors="ignore",
        )