"""
config.py

Configuración general del proyecto: constantes de negocio para los
distintos módulos de RPA sobre SAP (F-53, FB03, MIRO, etc.).

Se centraliza aquí, igual que sap_exceptions.py y models/sap_models.py,
para que cualquier módulo pueda reutilizar la configuración compartida
(ej. company_code) sin duplicarla.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SAPGeneralConfig:
    """Constantes de negocio compartidas entre transacciones SAP."""
    company_code: str = "1000"
    chrome_channel: str | None = "chrome"
    headless: bool = False
    navigation_timeout_ms: int = 30_000
    field_timeout_ms: int = 10_000
    dialog_timeout_ms: int = 10_000

@dataclass(frozen=True)
class FBRAConfig:
    """Constantes de negocio para FBRA (Anular compensaciones)."""
    transaction: str = "FBRA"
    company_code: str = "1000"
    reversal_reason: str = "01"
    text: str = "AJUSTE"

@dataclass(frozen=True)
class F03Config(SAPGeneralConfig):
    """Constantes de negocio para F-03 (Compensar contrapartidas de deudor)."""
    transaction: str = "F-03"
    general_ledger_account: str = "1110050302"
    currency: str = "COP"
    float_epsilon: float = 0.01  # tolerancia solo para ruido de punto flotante

    # Diligenciamiento inicial (_fill_header_fields)
    document_number_label: str = "Nº documento"
    process_items_label: str = "Tratar PAs"
    accounting_period_dialog_text: str = "Período contable"

    # Eliminar diferencias (_open_remove_differences_form / _remove_differences)
    remove_differences_label: str = "Eliminar diferencias"
    default_tax_indicator: str = "VZ"
    adjustment_text: str = "Ajuste al peso"
    adjustment_account: str = "5395950001"

    # Diálogos de confirmación (_batch_recorded / _no_open_items_found / etc.)
    batch_recorded_text: str = "Se grabaron los datos. Pueden entrarse más valores."
    no_open_items_text: str = "No se encontró ninguna posición de documento adecuada."
    selected_items_text: str = "partidas seleccionadas"
    clearing_posted_text: str = "se contabilizó en sociedad"

    # Contabilización (_post)
    post_button_label: str = "Contabilizar  Resaltado"

@dataclass(frozen=True)
class F53Config(SAPGeneralConfig):
    """Constantes de negocio específicas de la transacción F-53 (pago saliente)."""
    document_type: str = "KZ"
    currency: str = "COP"
    outgoing_bank_account: str = "1110050302"
    account_type: str = "D"
    transaction: str = "F-53"
    normal_items_label: str = "PAs normales"
    document_number_label: str = "Nº documento"
    process_items_label: str = "Tratar PAs"
    other_account_label: str = "Otra cuenta"
    post_button_label: str = "Contabilizar  Resaltado"
    batch_recorded_text: str = "Se grabaron los datos. Pueden entrarse más valores."
    no_open_items_text: str = "No se encontró ninguna posición de documento adecuada."
    selected_items_text: str = "partidas seleccionadas"
    added_items_text: str = "Se seleccionaron adicionalmente"
    clearing_posted_text: str = "se contabilizó en sociedad"


@dataclass(frozen=True)
class FAGLL03Config(SAPGeneralConfig):
    """Configuración de la consulta FAGLL03."""
    transaction: str = "FAGLL03"
    general_ledger_account: str = "1110050302"
    layout: str = "/BOT_COMP_ON"
    # FAGLL03 puede tardar varios minutos en devolver el resultado
    # tras F8 (partidas abiertas y, sobre todo, partidas compensadas
    # con rango de fecha amplio), igual que FBL5N.
    max_result_attempts: int = 600
    result_poll_interval_ms: int = 500


@dataclass(frozen=True)
class FBL5NConfig(SAPGeneralConfig):
    """Configuración de la consulta FBL5N."""
    transaction: str = "FBL5N"
    information_dialog_label: str = "Diálogo de información"
    no_items_text: str = "No se ha seleccionado ninguna"
    items_found_pattern: str = r"Se visualizan.*\d+"
    company_code_field_name: str = "Sociedad"
    layout: str = "/BOT_COMP_ON"
    # FBL5N puede tardar varios minutos en devolver el resultado.
    max_result_attempts: int = 600
    result_poll_interval_ms: int = 500