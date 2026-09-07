"""
sap_config.py

Configuración específica de cada transacción SAP automatizada (F-03, F-53,
FBRA, FAGLL03, FBL5N). Los valores de negocio compartidos (sociedad, moneda,
cuentas, tolerancias) NO se repiten aquí: se toman de `core.configuration.CONFIG`,
que es la fuente única de verdad. Este archivo solo agrega lo específico de
cada transacción: labels de campos/botones, textos de diálogos SAP, timeouts
de polling, etc.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.configuration import CONFIG


@dataclass(frozen=True)
class SAPReportColumns:
    """Nombres de columnas producidas por los reportes SAP descargados."""

    account: str = "Cuenta"
    document_number: str = "Nº documento"
    local_amount: str = "Importe en moneda local"
    text: str = "Texto"
    document_date: str = "Fecha de documento"
    clearing_document: str = "Doc.compensación"
    clearing_date: str = "Fecha compensación"
    customer_id: str = "identificacion_cliente"


REPORT_COLUMNS = SAPReportColumns()


# ======================================================================
# CONFIGURACIÓN BASE COMPARTIDA POR TODAS LAS TRANSACCIONES
# ======================================================================

@dataclass(frozen=True)
class SAPGeneralConfig:
    """Constantes compartidas por cualquier automatización SAP (browser, sociedad)."""

    company_code: str = CONFIG.sap.company_code
    chrome_channel: str | None = "chrome"
    headless: bool = False
    navigation_timeout_ms: int = 30_000
    field_timeout_ms: int = 10_000
    dialog_timeout_ms: int = 10_000


# ======================================================================
# TRANSACCIONES DE COMPENSACIÓN DE PARTIDAS ABIERTAS (F-03, F-53)
# ======================================================================

@dataclass(frozen=True)
class ClearingConfig(SAPGeneralConfig):
    """
    Constantes compartidas por las transacciones de compensación de partidas
    abiertas (F-03, F-53): mismos labels de campos/botones y mismos textos
    de diálogo de confirmación, independientemente de la transacción.
    """

    document_number_label: str = "Nº documento"
    process_items_label: str = "Tratar PAs"
    post_button_label: str = "Contabilizar  Resaltado"  # doble espacio: confirmado como name accesible real en SAP GUI

    batch_recorded_text: str = "Se grabaron los datos. Pueden entrarse más valores."
    no_open_items_text: str = "No se encontró ninguna posición de documento adecuada."
    selected_items_text: str = "partidas seleccionadas"
    clearing_posted_text: str = "se contabilizó en sociedad"


@dataclass(frozen=True)
class F03Config(ClearingConfig):
    """Constantes de negocio para F-03 (Compensar contrapartidas de deudor)."""

    transaction: str = "F-03"
    general_ledger_account: str = CONFIG.sap.outgoing_bank_account
    currency: str = CONFIG.sap.currency
    tolerance_for_cleared_items: int = CONFIG.rules.tolerance_for_cleared_items
    float_epsilon: float = CONFIG.rules.float_epsilon
    adjustment_document_class_debit: str = CONFIG.rules.adjustment_document_class_debit
    adjustment_document_class_credit: str = CONFIG.rules.adjustment_document_class_credit

    # Diligenciamiento inicial (_fill_header_fields)
    accounting_period_dialog_text: str = "Período contable"

    # Eliminar diferencias (_open_remove_differences_form / _remove_differences)
    remove_differences_label: str = "Eliminar diferencias"
    default_tax_indicator: str = CONFIG.rules.adjustment_tax_indicator
    adjustment_text: str = CONFIG.rules.adjustment_text
    adjustment_account: str = CONFIG.rules.adjustment_account
    


@dataclass(frozen=True)
class F53Config(ClearingConfig):
    """Constantes de negocio específicas de la transacción F-53 (pago saliente)."""

    transaction: str = "F-53"
    document_type: str = CONFIG.sap.document_type
    currency: str = CONFIG.sap.currency
    outgoing_bank_account: str = CONFIG.sap.outgoing_bank_account
    account_type: str = CONFIG.sap.account_type

    normal_items_label: str = "PAs normales"
    other_account_label: str = "Otra cuenta"
    added_items_text: str = "Se seleccionaron adicionalmente"


# ======================================================================
# ANULACIÓN DE COMPENSACIONES (FBRA)
# ======================================================================

@dataclass(frozen=True)
class FBRAConfig(SAPGeneralConfig):
    """Constantes de negocio para FBRA (Anular compensaciones)."""

    transaction: str = "FBRA"
    reversal_reason: str = CONFIG.rules.reversal_reason
    text: str = CONFIG.rules.reversal_text


# ======================================================================
# CONSULTAS DE REPORTE (FAGLL03, FBL5N)
# ======================================================================

@dataclass(frozen=True)
class ReportQueryConfig(SAPGeneralConfig):
    """
    Constantes compartidas por las consultas de reporte que pueden tardar
    varios minutos en devolver resultado (FAGLL03, FBL5N con rango de fecha
    amplio o muchas partidas): mismo layout guardado y mismo esquema de polling.
    """

    layout: str = "/BOT_COMP_ON"
    max_result_attempts: int = 600
    result_poll_interval_ms: int = 500


@dataclass(frozen=True)
class FAGLL03Config(ReportQueryConfig):
    """Configuración de la consulta FAGLL03."""

    transaction: str = "FAGLL03"
    general_ledger_account: str = CONFIG.sap.outgoing_bank_account
    tolerance: int = CONFIG.rules.tolerance
    cleared_items_lookback_months: int = CONFIG.rules.cleared_items_lookback_months


@dataclass(frozen=True)
class FBL5NConfig(ReportQueryConfig):
    """Configuración de la consulta FBL5N."""

    transaction: str = "FBL5N"
    information_dialog_label: str = "Diálogo de información"
    no_items_text: str = "No se ha seleccionado ninguna"
    items_found_pattern: str = r"Se visualizan.*\d+"
    company_code_field_name: str = "Sociedad"