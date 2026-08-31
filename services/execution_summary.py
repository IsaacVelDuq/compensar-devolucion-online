from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil


@dataclass(slots=True)
class SAPOperationResult:
    """Resultado legible de una operación SAP, sin acoplarla a la interfaz."""

    operation: str
    success: bool
    message: str
    document: str | None = None
    error: str | None = None


@dataclass
class ExecutionSummary:
    """Acumula resultados y produce el resumen destinado a Tesorería."""

    payment_date: datetime
    bank_voucher: Path
    online_refunds: Path
    started_at: datetime = field(default_factory=datetime.now)
    operations: list[SAPOperationResult] = field(default_factory=list)
    package_dir: Path | None = None

    def __post_init__(self) -> None:
        """Acepta rutas provenientes tanto de argparse como de la GUI."""
        self.bank_voucher = Path(self.bank_voucher)
        self.online_refunds = Path(self.online_refunds)

    def add_success(self, operation: str, message: str, document: str | None = None) -> None:
        self.operations.append(SAPOperationResult(operation, True, message, document))

    def add_error(self, operation: str, error: BaseException | str, document: str | None = None) -> None:
        self.operations.append(
            SAPOperationResult(operation, False, str(error), document, str(error))
        )

    @property
    def succeeded(self) -> int:
        return sum(item.success for item in self.operations)

    @property
    def failed(self) -> int:
        return sum(not item.success for item in self.operations)

    def write_package(self, output_dir: Path) -> Path:
        payment = self.payment_date.strftime("%d-%m-%Y")
        package_dir = Path(output_dir) / f"compensacion_dev_online_{payment}"
        package_dir.mkdir(parents=True, exist_ok=True)
        for source, target_name in (
            (self.online_refunds, f"devoluciones_online{self.online_refunds.suffix}"),
            (self.bank_voucher, f"comprobante_banco{self.bank_voucher.suffix}"),
        ):
            if source.exists():
                shutil.copy2(source, package_dir / target_name)
        (package_dir / "resumen.txt").write_text(self.render(), encoding="utf-8")
        self.package_dir = package_dir
        return package_dir

    def render(self) -> str:
        lines = [
            "RESUMEN DE EJECUCIÓN",
            "=" * 24,
            "Proceso: Compensación devoluciones Online",
            f"Fecha de ejecución: {self.started_at:%d/%m/%Y %H:%M:%S}",
            f"Fecha de pago: {self.payment_date:%d/%m/%Y}",
            "",
            "ARCHIVOS PROCESADOS",
            "-" * 19,
            f"Devoluciones Online: {self.online_refunds.name}",
            f"Comprobante banco: {self.bank_voucher.name}",
            "",
            "OPERACIONES REALIZADAS",
            "-" * 23,
        ]
        for item in self.operations:
            state = "OK" if item.success else "ERROR"
            lines.append(f"[{state}] {item.operation}")
            if item.document:
                lines.append(f"Documento SAP: {item.document}")
            lines.append(f"Mensaje SAP: {item.message}")
            lines.append("")
        if not self.operations:
            lines.append("No se alcanzaron a ejecutar operaciones SAP.")
            lines.append("")
        lines.extend([
            "RESULTADO",
            "-" * 9,
            f"Operaciones exitosas: {self.succeeded}",
            f"Operaciones con error: {self.failed}",
            "El proceso finalizó correctamente." if not self.failed else "El proceso finalizó con errores.",
        ])
        return "\n".join(lines) + "\n"
