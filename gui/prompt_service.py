from __future__ import annotations

import queue
from dataclasses import dataclass, field


@dataclass
class PromptRequest:
    """Una solicitud de decisión del usuario, generada desde el worker."""

    title: str
    message: str
    timeout: float
    # maxsize=1: solo cabe una respuesta; es justo lo que necesitamos.
    response: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=1))


class GuiPromptService:
    """Implementa el contrato `core.prompt_service.PromptService` para la
    GUI de escritorio.

    Puente thread-safe entre el worker de SAP (hilo en background) y la
    GUI (hilo principal de Tkinter) para mostrar diálogos modales de tipo
    Reintentar / Cancelar.

    Por qué existe: `services.orchestrator.run()` corre en un
    `threading.Thread` separado del hilo principal de Tkinter. Tkinter NO es
    thread-safe: solo el hilo principal puede crear o tocar widgets. Por
    eso el worker no puede abrir un `CTkToplevel` directamente — en su lugar
    encola una `PromptRequest` aquí, bloquea SU PROPIO hilo esperando una
    respuesta, y es la GUI (vía `after(...)`, que ya corre en el hilo
    principal) quien detecta la solicitud, dibuja el diálogo y deposita la
    respuesta.

    El timeout de espera lo controla la GUI (ver `gui/app.py`): si pasan
    5 minutos sin que el usuario pulse un botón, la propia GUI responde
    "cancel" automáticamente. `ask_retry_cancel` incluye un timeout propio
    ligeramente mayor solo como red de seguridad, por si la GUI llegara a
    no responder por algún motivo.
    """

    def __init__(self) -> None:
        self.pending_requests: "queue.Queue[PromptRequest]" = queue.Queue()

    def ask_retry_cancel(self, title: str, message: str, timeout: float = 300) -> str:
        """Bloquea el hilo llamante (el worker) hasta obtener "retry" o
        "cancel". Nunca lanza: ante cualquier fallo de comunicación con la
        GUI, devuelve "cancel" (falla hacia el lado seguro)."""
        request = PromptRequest(title=title, message=message, timeout=timeout)
        self.pending_requests.put(request)
        try:
            # +10s de margen sobre el timeout que ya aplica la GUI, como
            # último resguardo si la GUI no llegara a responder.
            return request.response.get(timeout=timeout + 10)
        except queue.Empty:
            return "cancel"