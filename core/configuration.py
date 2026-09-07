"""
core.configuration

Configuración central del proyecto de automatización SAP:

1. Utilidades de ruta/persistencia para `config.json` (valores que el
   usuario puede cambiar sin tocar código: credenciales y rutas de
   archivos de entrada).
2. Constantes de negocio y de conexión SAP, agrupadas por dominio en
   dataclasses inmutables para que cada módulo importe solo lo que
   necesita (`CONFIG.sap`, `CONFIG.rules`, `CONFIG.report`, etc.).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


# ======================================================================
# RUTAS DE LA APLICACIÓN Y PERSISTENCIA DE config.json
# ======================================================================

def application_dir() -> Path:
    """Carpeta del ejecutable con PyInstaller o raíz del proyecto en desarrollo."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    return application_dir() / "config.json"


def load_config() -> dict[str, str]:
    """Carga la configuración editable por el usuario desde config.json."""
    path = config_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def save_config(values: dict[str, str]) -> None:
    """Guarda la configuración elegida por el usuario junto a la aplicación."""
    with config_path().open("w", encoding="utf-8") as file:
        json.dump(values, file, ensure_ascii=False, indent=2)


# ======================================================================
# CONFIGURACIÓN EDITABLE POR EL USUARIO (persistida en config.json)
# ======================================================================

@dataclass(frozen=True)
class UserConfig:
    """
    Valores que dependen del usuario/equipo y NO deben quedar hardcodeados
    en el código: credenciales SAP y rutas a los archivos de entrada que
    cada quien tiene en su máquina.
    """

    sap_user: str = ""
    sap_password: str = ""
    bank_voucher: Path = field(default_factory=Path)
    online_refunds: Path = field(default_factory=Path)

    @classmethod
    def from_saved(cls) -> "UserConfig":
        """Construye la configuración de usuario a partir de config.json (si existe)."""
        data = load_config()
        return cls(
            sap_user=data.get("SAP_USER", ""),
            sap_password=data.get("SAP_PASSWORD", ""),
            bank_voucher=Path(data["BANK_VOUCHER"]) if data.get("BANK_VOUCHER") else Path(),
            online_refunds=Path(data["ONLINE_REFUNDS"]) if data.get("ONLINE_REFUNDS") else Path(),
        )

    def to_dict(self) -> dict[str, str]:
        """Serializa a un dict compatible con `save_config`."""
        return {
            "SAP_USER": self.sap_user,
            "SAP_PASSWORD": self.sap_password,
            "BANK_VOUCHER": str(self.bank_voucher),
            "ONLINE_REFUNDS": str(self.online_refunds),
        }


# ======================================================================
# CONEXIÓN Y DATOS FIJOS DE SAP
# ======================================================================

@dataclass(frozen=True)
class SAPConnection:
    """Datos fijos de conexión y maestros SAP (sociedad, cuentas, moneda)."""

    url: str = "https://saps4h.gco.com.co/sap/bc/gui/sap/its/webgui/?sap-client=300&sap-language=ES#"
    company_code: str = "1000"
    customer_accounts: tuple[str, ...] = ("20001", "1000004288")
    currency: str = "COP"
    document_type: str = "KZ"
    account_type: str = "D"
    outgoing_bank_account: str = "1110050302"

    @property
    def customer_accounts_input(self) -> str:
        """
        Texto listo para pegar en un campo SAP multivalor (una cuenta por
        línea), equivalente al antiguo `CUSTOMER_ACCOUNT = "20001\\n1000004288"`.
        """
        return "\n".join(self.customer_accounts)


# ======================================================================
# REGLAS DE NEGOCIO / TOLERANCIAS
# ======================================================================

@dataclass(frozen=True)
class BusinessRules:
    """Tolerancias y reglas de negocio compartidas entre matchers."""

    tolerance: int = 1_000
    tolerance_for_cleared_items: int = 100
    bank_voucher_tolerance: float = 10.0
    float_epsilon: float = 0.01  # tolerancia solo para ruido de punto flotante, no de negocio
    included_bank_payments: tuple[str, ...] = ("PAGO EXITOSO Y ABONADO",)
    cleared_items_lookback_months: int = 1
    adjustment_document_class_debit: str = "50"
    adjustment_document_class_credit: str = "40"
    adjustment_tax_indicator: str = "VZ"
    adjustment_text: str = "Ajuste al peso"
    adjustment_account: str = "5395950001"
    reversal_reason: str = "01"
    reversal_text: str = "AJUSTE"
    payment_text_template: str = "Dev Online {day}"


@dataclass(frozen=True)
class InputColumns:
    """Nombres de columnas de los archivos de entrada del proceso."""

    bank_document: str = "Documento Beneficiario"
    bank_result: str = "Descripción código resultado"
    bank_amount: str = "Valor"
    bank_transmission_date: str = "Fecha de transmisión"
    online_customer_id: str = "Cédula cliente o nit"
    online_amount: str = "Valor devolución o saldo"
    online_accounting_document: str = "Doc contable"


# ======================================================================
# REPORTES / SALIDAS
# ======================================================================

@dataclass(frozen=True)
class ReportSettings:
    """Configuración de generación y descarga de reportes SAP."""

    layout: str = "/BOT COMPENS"
    output_dir: Path = field(default_factory=lambda: Path("downloads"))
    output_subdir: str = "Reportes_SAP"
    report_name: str = "fbl5n_report_TEMP"
    fagll03_report_name: str = "fagll03"
    cleared_items_report_name: str = "fagll03_compensadas"
    report_date: pd.Timestamp = field(default_factory=pd.Timestamp.now)


# ======================================================================
# CONFIGURACIÓN CONSOLIDADA
# ======================================================================

@dataclass(frozen=True)
class AppConfig:
    """Punto único de acceso a toda la configuración del proyecto."""

    user: UserConfig = field(default_factory=UserConfig.from_saved)
    sap: SAPConnection = field(default_factory=SAPConnection)
    rules: BusinessRules = field(default_factory=BusinessRules)
    inputs: InputColumns = field(default_factory=InputColumns)
    report: ReportSettings = field(default_factory=ReportSettings)


CONFIG = AppConfig()