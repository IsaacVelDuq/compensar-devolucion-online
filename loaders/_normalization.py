import pandas as pd


def normalize_identifier(series: pd.Series) -> pd.Series:
    """Normaliza identificadores conservando ceros y quitando .0 de Excel."""
    normalized = (
        series.astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .replace(r"^\s*$", pd.NA, regex=True)
    )
    numeric_mask = normalized.str.fullmatch(r"\d+", na=False)
    normalized.loc[numeric_mask] = normalized.loc[numeric_mask].str.lstrip("0")
    normalized.loc[numeric_mask & normalized.eq("")] = "0"
    return normalized


def normalize_money(series: pd.Series) -> pd.Series:
    """Convierte importes con formatos Excel, COP y decimal internacional."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    values = series.astype("string").str.strip().str.replace("$", "", regex=False)
    values = values.str.replace(r"\s+", "", regex=True)
    both_separators = values.str.contains(r"[.,].*[.,]", regex=True, na=False)
    comma_decimal = values.str.rfind(",") > values.str.rfind(".")
    values = values.where(
        ~both_separators | ~comma_decimal,
        values.str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
    )
    values = values.where(
        ~both_separators | comma_decimal,
        values.str.replace(",", "", regex=False),
    )
    values = values.where(
        both_separators,
        values.str.replace(",", ".", regex=False),
    )
    return pd.to_numeric(values, errors="coerce")


def normalize_date(series: pd.Series) -> pd.Series:
    """Convierte fechas Excel, DDMMYYYY, YYYYMMDD y datetime sin conservar la hora."""

    result = pd.Series(
        pd.NaT,
        index=series.index,
        dtype="datetime64[ns]",
    )

    # ----------------------------------------------------------
    # 1. Datetime
    # ----------------------------------------------------------
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(
            series,
            errors="coerce",
        ).dt.normalize()

    # ----------------------------------------------------------
    # 2. Valores numéricos
    # ----------------------------------------------------------
    numeric = pd.to_numeric(series, errors="coerce")
    numeric_mask = series.notna() & numeric.notna()

    # ----------------------------------------------------------
    # 2.1 Fechas seriales de Excel
    # Ejemplo: 45500
    # ----------------------------------------------------------
    excel_mask = (
        numeric_mask
        & numeric.between(1, 60000)
    )

    if excel_mask.any():
        result.loc[excel_mask] = pd.to_datetime(
            numeric.loc[excel_mask],
            unit="D",
            origin="1899-12-30",
            errors="coerce",
        )

    # ----------------------------------------------------------
    # 2.2 Fechas numéricas de 8 dígitos
    # Soporta:
    #   DDMMYYYY → 19082026 = 19/08/2026
    #   YYYYMMDD → 20260819 = 19/08/2026
    # ----------------------------------------------------------
    numeric_text = (
        numeric.loc[numeric_mask & ~excel_mask]
        .round()
        .astype("Int64")
        .astype("string")
        .str.zfill(8)
    )

    numeric_date_mask = numeric_text.str.fullmatch(
        r"\d{8}",
        na=False,
    )

    if numeric_date_mask.any():

        # ------------------------------------------------------
        # Primero intentamos DDMMYYYY
        # ------------------------------------------------------
        dmy_mask = (
            numeric_date_mask
            & pd.to_numeric(
                numeric_text.str[:2],
                errors="coerce",
            ).between(1, 31)
            & pd.to_numeric(
                numeric_text.str[2:4],
                errors="coerce",
            ).between(1, 12)
        )

        if dmy_mask.any():
            result.loc[
                numeric_text.index[dmy_mask]
            ] = pd.to_datetime(
                numeric_text.loc[dmy_mask],
                format="%d%m%Y",
                errors="coerce",
            )

        # ------------------------------------------------------
        # Los que no fueron DDMMYYYY se intentan como YYYYMMDD
        # ------------------------------------------------------
        ymd_mask = (
            numeric_date_mask
            & ~dmy_mask
            & numeric_text.str.match(
                r"(?:19|20)\d{2}",
                na=False,
            )
        )

        if ymd_mask.any():
            result.loc[
                numeric_text.index[ymd_mask]
            ] = pd.to_datetime(
                numeric_text.loc[ymd_mask],
                format="%Y%m%d",
                errors="coerce",
            )

    # ----------------------------------------------------------
    # 3. Fechas almacenadas como texto
    # Ejemplo:
    #   "19/08/2026"
    #   "19-08-2026"
    # ----------------------------------------------------------
    text_mask = series.notna() & ~numeric_mask

    if text_mask.any():
        result.loc[text_mask] = pd.to_datetime(
            series.loc[text_mask],
            dayfirst=True,
            errors="coerce",
        )

    # ----------------------------------------------------------
    # 4. Eliminar la hora
    # ----------------------------------------------------------
    return result.dt.normalize()

