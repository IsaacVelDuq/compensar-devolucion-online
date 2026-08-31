"""
sap_exceptions.py

Jerarquía de excepciones para módulos de RPA sobre SAP WebGUI (Playwright).

Se centraliza aquí a propósito: la idea es que TODOS los módulos de RPA
que vayas construyendo sobre SAP (FBL5N, FB03, MIRO, lo que sea) hereden
de `SAPRPAError` para sus errores específicos. Así, un llamador que
orqueste varios módulos puede hacer:

    try:
        resultado = generador.generate("reporte")
    except SAPValidationError as e:
        # dato de entrada rechazado por SAP
        ...
    except SAPRPAError as e:
        # cualquier otro fallo de automatización, de cualquier módulo
        ...

y siempre obtiene un mensaje ya legible para mostrar al usuario final,
sin tener que parsear strings tipo "ERROR: ..." o "SIN_PARTIDAS: ...".
"""

from __future__ import annotations


class SAPRPAError(Exception):
    """Excepción base para cualquier error ocurrido durante una
    automatización RPA sobre SAP WebGUI.

    No se lanza directamente: se usa como clase base para que el
    llamador pueda capturar cualquier falla de cualquier módulo de RPA
    con un solo `except SAPRPAError`.
    """


class SAPValidationError(SAPRPAError):
    """SAP rechazó uno o más valores ingresados en el formulario.

    Ejemplo típico: un código de sociedad o una cuenta que no existe,
    detectado porque el campo queda marcado con aria-invalid="true".
    El mensaje ya viene con la explicación que SAP mostró (o una
    genérica legible si SAP no dio detalle).
    """


class SAPNoItemsFoundError(SAPRPAError):
    """La consulta se ejecutó correctamente, pero no arrojó partidas
    para los criterios seleccionados. No es un error técnico: es un
    resultado de negocio válido que el llamador debe decidir cómo
    manejar (reintentar con otros criterios, notificar, etc.)."""


class SAPResultNotDetectedError(SAPRPAError):
    """No se pudo determinar qué ocurrió tras ejecutar la consulta
    (ni error de validación, ni ausencia de partidas, ni partidas
    encontradas) dentro del tiempo de espera configurado. Suele
    indicar que la interfaz de SAP cambió o está respondiendo distinto
    a lo esperado."""


class SAPReportDownloadError(SAPRPAError):
    """La consulta tuvo partidas, pero el reporte no pudo descargarse
    (el popup de exportación no apareció, el archivo quedó vacío,
    etc.)."""


class SAPAutomationError(SAPRPAError):
    """Fallo técnico durante la interacción con la interfaz de SAP
    (un elemento no apareció, un botón no se encontró, un timeout no
    atribuible a una regla de negocio, etc.). Es el "cajón general"
    para errores que no encajan en las categorías anteriores, pero
    siempre con un mensaje ya redactado para el usuario final."""


class SAPDocumentLockedError(SAPRPAError):
    """SAP impidió anular un documento porque otro usuario lo tiene bloqueado."""

    def __init__(self, document: str, sap_message: str):
        self.document = document
        self.sap_message = sap_message
        super().__init__(
            f"El documento {document} está bloqueado por otro usuario. "
            f"Mensaje SAP: {sap_message}"
        )
