from __future__ import annotations

import threading
from typing import Protocol


class PromptService(Protocol):
    """Contrato para pedirle al usuario una decisión Reintentar/Cancelar
    ante un error puntual durante la automatización SAP.

    `services.orchestrator` depende ÚNICAMENTE de este contrato, nunca
    de una implementación concreta. Así la lógica de negocio no se acopla
    a Tkinter (GUI) ni a ninguna interfaz en particular: la GUI vive en
    `gui/prompt_service.py` (`GuiPromptService`) y aquí solo queda una
    implementación mínima de respaldo para ejecución por CLI.
    """

    def ask_retry_cancel(self, title: str, message: str, timeout: float = 300) -> str:
        """Debe devolver "retry" o "cancel".

        Debe resolver siempre dentro de `timeout` segundos (o antes, si el
        usuario responde primero); si no hay respuesta a tiempo, debe
        resolver a "cancel". Nunca debe lanzar una excepción propia."""
        ...


class ConsolePromptService:
    """Implementación de respaldo para ejecución por línea de comandos
    (sin GUI disponible). Pregunta por consola con el mismo límite de
    tiempo; cualquier cosa que no sea una respuesta explícita de
    reintentar, incluyendo el timeout, se trata como cancelar."""

    def ask_retry_cancel(self, title: str, message: str, timeout: float = 300) -> str:
        print(f"\n[{title}]\n{message}")
        answer: dict[str, str] = {}

        def _read() -> None:
            try:
                answer["value"] = input(
                    "¿Reintentar? [r = reintentar / cualquier otra tecla = cancelar]: "
                ).strip().lower()
            except EOFError:
                answer["value"] = "cancel"

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout)

        if reader.is_alive():
            print("Sin respuesta a tiempo, se cancela automáticamente.")
            return "cancel"

        return "retry" if answer.get("value") == "r" else "cancel"