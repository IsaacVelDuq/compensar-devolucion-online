"""
report_generator.py

Genera reportes contables desde SAP WebGUI (transacción FBL5N) usando
Playwright (API síncrona), reutilizando una página ya autenticada.

Tras ejecutar la consulta (F8), detecta cuál de tres resultados ocurrió
primero:

    - ERROR       -> el campo 'Sociedad' quedó con aria-invalid="true"
    - NO_ITEMS    -> el diálogo de información indica que no hay partidas
    - ITEMS_FOUND -> aparece "Se visualizan ... <n>" en pantalla

El método público `generate()` retorna únicamente `Path` cuando el
reporte se descarga correctamente. Cualquier otra situación (error de
validación, ausencia de partidas, fallo técnico) se comunica lanzando
una excepción de `sap_exceptions`, con un mensaje ya redactado para
mostrarle al usuario final. No hay retornos tipo "ERROR: ..." ni
"SIN_PARTIDAS: ...": el llamador debe manejar estos casos con
try/except, no parseando strings.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path
from typing import Final, Sequence

import pyperclip
import pandas as pd
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from .sap_exceptions import (
    SAPAutomationError,
    SAPNoItemsFoundError,
    SAPReportDownloadError,
    SAPResultNotDetectedError,
    SAPRPAError,
    SAPValidationError,
)
from .sap_config import FBL5NConfig
from .sap_error_recorder import ensure_error_columns, record_error



logger = logging.getLogger("estado_cuenta")


# ---------------------------------------------------------------------------
# RESULTADO DE LA EJECUCIÓN (F8)
# ---------------------------------------------------------------------------

class ExecutionResult(str, Enum):
    """Posibles desenlaces tras ejecutar la consulta en SAP (F8)."""

    ERROR = "ERROR"
    NO_ITEMS = "NO_ITEMS"
    ITEMS_FOUND = "ITEMS_FOUND"


# Textos y patrones utilizados para detectar los resultados de SAP.
class FBL5NReportGenerator:
    """
    Genera reportes contables desde SAP utilizando la transacción FBL5N.

    Reutiliza una página de navegador ya autenticada mediante una sesión
    compartida.
    """

    def __init__(
        self,
        page: Page,
        company_code: str,
        customer_account: str,
        date: str,
        layout: str,
        output_dir: Path,
        document_numbers_to_unlock: Sequence[str] | None = None,
        max_result_attempts: int = 60,
        result_poll_interval_ms: int = 500,
        config: FBL5NConfig | None = None,
        df_errores: pd.DataFrame | None = None,
    ):
        self.page = page
        self.company_code = company_code
        self.customer_account = customer_account
        self.date = date
        self.layout = layout
        self.output_dir = output_dir
        self.config = config or FBL5NConfig(
            company_code=company_code,
            max_result_attempts=max_result_attempts,
            result_poll_interval_ms=result_poll_interval_ms,
        )
        self.company_code = self.config.company_code
        self.layout = self.config.layout
        self.df_errores = ensure_error_columns(df_errores)

        # Documentos a desbloquear en el flujo de filtrado. Si no se
        # especifican, se usa el conjunto por defecto (comportamiento
        # original preservado).
        self.document_numbers_to_unlock = (
            list(document_numbers_to_unlock))


        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # Parámetros utilizados para sondear el resultado después de F8.
        self.max_result_attempts = self.config.max_result_attempts
        self.result_poll_interval_ms = self.config.result_poll_interval_ms

        logger.info(
            "Inicializando ReportGenerator | "
            f"company_code={self.company_code} | "
            f"customer_account={self.customer_account} | "
            f"date={self.date} | "
            f"layout={self.layout} | "
            f"output_dir={self.output_dir} | "
            f"document_numbers_to_unlock={self.document_numbers_to_unlock}"
        )

    # -----------------------------------------------------------------------
    # PUNTO DE ENTRADA PÚBLICO
    # -----------------------------------------------------------------------

    def generate(self, report_name: str) -> Path:
        """
        Ejecuta el flujo completo de FBL5N: navegación, diligenciamiento
        de campos, ejecución de la consulta y descarga del reporte.

        Returns:
            Path: ruta del archivo descargado.

        Raises:
            SAPValidationError: SAP marcó un error de validación en el
                formulario (p. ej. sociedad o cuenta inexistente).
            SAPNoItemsFoundError: la consulta se ejecutó correctamente
                pero no se encontraron partidas.
            SAPResultNotDetectedError: no se pudo determinar el resultado
                de la consulta dentro del tiempo de espera configurado.
            SAPReportDownloadError: se encontraron partidas pero el
                reporte no pudo descargarse.
            SAPAutomationError: cualquier otro fallo técnico durante la
                automatización (elemento no encontrado, timeout no
                atribuible a una regla de negocio, etc.)
        """

        logger.info(f"Iniciando generación de reporte | report_name={report_name}")
        try:
            self._navigate_to_fbl5n()
            self._fill_fields()
            result, message = self._execute_query()
        except SAPRPAError as error:
            self._record_error("generate", error)
            raise
        except PlaywrightTimeoutError as error:
            wrapped_error = SAPAutomationError(
                "SAP no respondió a tiempo mientras se diligenciaban los "
                f"campos de la consulta. Detalle técnico: {error}"
            )
            self._record_error("generate", wrapped_error)
            raise wrapped_error from error
        except Exception as error:
            wrapped_error = SAPAutomationError(
                "Ocurrió un error inesperado preparando la consulta en "
                f"SAP: {error}"
            )
            self._record_error("generate", wrapped_error)
            raise wrapped_error from error

        if result is ExecutionResult.ERROR:
            error = SAPValidationError(message)
            self._record_error("generate", error)
            raise error
        if result is ExecutionResult.NO_ITEMS:
            error = SAPNoItemsFoundError(message)
            self._record_error("generate", error)
            raise error

        try:
            self._filter_and_unlock_documents()
            output_path = self._download_report(report_name)
        except SAPRPAError as error:
            self._record_error("generate", error)
            raise
        except PlaywrightTimeoutError as error:
            wrapped_error = SAPReportDownloadError(
                "SAP no respondió a tiempo durante la descarga del "
                f"reporte '{report_name}'. Detalle técnico: {error}"
            )
            self._record_error("generate", wrapped_error)
            raise wrapped_error from error
        except Exception as error:
            wrapped_error = SAPReportDownloadError(
                f"No se pudo descargar el reporte '{report_name}': {error}"
            )
            self._record_error("generate", wrapped_error)
            raise wrapped_error from error

        logger.info(f"Reporte generado correctamente | path={output_path}")
        return output_path

    def _record_error(self, operation: str, error: BaseException) -> None:
        record_error(self.df_errores, self.__class__.__name__, operation, error)

    def get_errors(self) -> pd.DataFrame:
        """Devuelve los errores visibles registrados durante la generación."""
        return self.df_errores

    # -----------------------------------------------------------------------
    # NAVEGACIÓN
    # -----------------------------------------------------------------------

    def _navigate_to_fbl5n(self):
        """Ingresa a la transacción FBL5N."""

        logger.info("Navegando a transacción FBL5N")
        try:
            ok_code_field = self.page.locator(
                "input[id='ToolbarOkCode']"
            )

            ok_code_field.wait_for(
                state="visible",
                timeout=30_000,
            )

            ok_code_field.click()
            ok_code_field.fill(f"/n{self.config.transaction}")

            self.page.keyboard.press("Enter")

            self.page.wait_for_load_state("networkidle")

            logger.info(
                "Transacción FBL5N cargada correctamente"
            )

        except Exception as error:
            logger.exception(
                f"Error navegando a FBL5N: {error}"
            )
            raise

    # -----------------------------------------------------------------------
    # DILIGENCIAMIENTO DE CAMPOS
    # -----------------------------------------------------------------------

    def _fill_fields(self):
        """Diligencia los campos requeridos para ejecutar la consulta FBL5N."""

        logger.info("Iniciando llenado de campos")

        try:
            if not self.customer_account:
                logger.error("La cuenta de cliente está vacía")
                raise SAPValidationError(
                    "La cuenta de cliente está vacía. Verifique el "
                    "parámetro 'customer_account' antes de reintentar."
                )

            self._fill_company_code()
            self._fill_customer_account()

            logger.info("Seleccionando partidas abiertas y checkboxes asociados")

            open_items_radio = self.page.get_by_role(
                "radio", name="Partidas abiertas"
            )
            open_items_radio.wait_for(state="visible", timeout=10_000)
            open_items_radio.check()
            logger.info("Radio 'Partidas abiertas' seleccionado")

            normal_items_checkbox = self.page.get_by_role(
                "checkbox", name="Operaciones CME"
            )
            normal_items_checkbox.wait_for(state="visible", timeout=10_000)
            normal_items_checkbox.check()
            logger.info("Checkbox 'Operaciones CME' (normal) marcado")

            cme_operations_checkbox = self.page.get_by_role(
                "checkbox", name="Operaciones CME"
            )
            cme_operations_checkbox.wait_for(state="visible", timeout=10_000)
            cme_operations_checkbox.check()
            logger.info("Checkbox 'Operaciones CME' marcado")

            statistical_entries_checkbox = self.page.get_by_role(
                "checkbox", name="Apuntes estadísticos"
            )
            statistical_entries_checkbox.wait_for(state="visible", timeout=10_000)
            statistical_entries_checkbox.check()
            logger.info("Checkbox 'Apuntes estadísticos' marcado")

            preliminary_entries_checkbox = self.page.get_by_role(
                "checkbox", name="Part.reg.forma preliminar"
            )
            preliminary_entries_checkbox.wait_for(state="visible", timeout=10_000)
            preliminary_entries_checkbox.uncheck()
            logger.info("Checkbox 'Part.reg.forma preliminar' desmarcado")

            credit_balance_items_checkbox = self.page.get_by_role(
                "checkbox", name="Part.saldo acreedor"
            )
            credit_balance_items_checkbox.wait_for(state="visible", timeout=10_000)
            credit_balance_items_checkbox.check()
            logger.info("Checkbox 'Part.saldo acreedor' marcado")

            logger.info(
                f"Ingresando fecha: {self.date.strftime('%d.%m.%Y')}"
            )

            date_field = self.page.get_by_role(
                "textbox", name="Abiertas en fe.clv."
            ).first

            date_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            date_field.fill(
                self.date.strftime("%d.%m.%Y")
            )

            logger.info(
                f"Ingresando layout: {self.layout}"
            )

            layout_field = self.page.get_by_role(
                "textbox",
                name="Layout",
            )

            layout_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            layout_field.fill(self.layout)

            logger.info(
                "Campos diligenciados correctamente"
            )

        except SAPRPAError:
            raise

        except Exception as error:
            logger.exception(
                f"Error llenando campos: {error}"
            )
            raise

    def _fill_company_code(self):
        """Diligencia el campo de sociedad."""

        logger.info("Diligenciando sociedad")

        try:
            company_code_field = self.page.locator(
                "input[title='Sociedad']"
            ).nth(0)

            company_code_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            company_code_field.fill(self.config.company_code)

            self.page.get_by_role(
                "button",
                name="Selección múltiple",
            ).nth(1).click()

            self._paste_into_dialog(
                self.company_code
            )

            logger.info("Sociedad diligenciada correctamente")

        except Exception as error:
            logger.exception(
                f"Error diligenciando sociedad: {error}"
            )
            raise

    def _fill_customer_account(self):
        """Diligencia el campo de cuenta de cliente."""

        logger.info("Diligenciando cuenta de cliente")

        try:
            account_field = self.page.get_by_role(
                "textbox",
                name="Cuenta de deudor",
            )

            account_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            self.page.get_by_role(
                "button",
                name="Selección múltiple",
            ).nth(0).click()

            self._paste_into_dialog(
                self.customer_account
            )

            logger.info("Cuenta de cliente diligenciada correctamente")

        except Exception as error:
            logger.exception(
                f"Error diligenciando cuenta de cliente "
                f"{self.customer_account}: {error}"
            )
            raise

        self.page.locator("body").click()

    # -----------------------------------------------------------------------
    # DIÁLOGO DE SELECCIÓN MÚLTIPLE
    # -----------------------------------------------------------------------

    def _paste_into_dialog(self, text: str, is_filter_dialog: bool = False):
        """
        Pega información en un diálogo de selección múltiple de SAP.

        Args:
            text: contenido a pegar (uno o varios valores separados por
                salto de línea).
            is_filter_dialog: indica si el diálogo corresponde al flujo de
                "Fijar filtros" sobre la columna Nº doc., que requiere pasos
                adicionales (abrir selección múltiple interna y ejecutar el
                filtro al finalizar).
        """

        logger.info(
            f"Iniciando pegado en diálogo SAP | is_filter_dialog={is_filter_dialog}"
        )

        dialog = self.page.locator(
            "div[role='dialog']"
        )

        try:
            dialog.wait_for(
                state="visible",
                timeout=10_000,
            )

            logger.info(
                "Diálogo SAP detectado"
            )

        except PlaywrightTimeoutError as error:
            logger.exception(
                "El diálogo de selección múltiple no apareció"
            )

            raise SAPAutomationError(
                "El diálogo de selección múltiple de SAP no apareció a "
                "tiempo."
            ) from error

        if is_filter_dialog:
            try:
                multi_select_button = self.page.get_by_role(
                    "button", name="Selección múltiple"
                )
                multi_select_button.wait_for(state="visible", timeout=10_000)
                multi_select_button.click()
                self.page.wait_for_load_state("networkidle", timeout=10_000)

                logger.info(
                    "Selección múltiple interna del filtro abierta correctamente"
                )

            except Exception as error:
                logger.exception(
                    "El diálogo de selección múltiple dentro del filtro no apareció"
                )

                raise SAPAutomationError(
                    "El diálogo de selección múltiple dentro del filtro "
                    "no apareció a tiempo."
                ) from error

            dialog = self.page.get_by_role(
                "dialog", name="Selección múltiple para Nº"
            )
            text_area = dialog.locator("input[name=\"InputField\"]")
        else:
            text_area = dialog.locator(
                "textarea, input[type='text']"
            ).first

        try:
            text_area.wait_for(
                state="visible",
                timeout=5_000,
            )

            text_area.click()

            logger.info(
                "Área de texto enfocada"
            )

        except PlaywrightTimeoutError:
            logger.warning(
                "No se encontró área de texto; usando fallback"
            )

            dialog.click()

        logger.info(
            "Limpiando contenido previo"
        )

        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")

        logger.info(
            "Pegando información en diálogo"
        )

        pyperclip.copy(text)
        self.page.keyboard.press("Shift+F12")

        self.page.wait_for_timeout(800)

        take_button = self._find_take_button(
            dialog
        )

        take_button.click()

        logger.info(
            "Botón Tomar presionado"
        )

        try:
            dialog.wait_for(
                state="hidden",
                timeout=8_000,
            )

            logger.info(
                "Diálogo cerrado correctamente"
            )

            if is_filter_dialog:
                execute_filter_button = self.page.get_by_role(
                    "button", name="Ejecutar (Entrada)"
                )
                execute_filter_button.wait_for(state="visible", timeout=10_000)
                execute_filter_button.click()

                logger.info(
                    "Filtro ejecutado correctamente"
                )

                self.page.wait_for_load_state("networkidle", timeout=10_000)
                self._confirm_ok_dialog()

        except PlaywrightTimeoutError as error:
            logger.exception(
                "El diálogo no se cerró"
            )

            raise SAPAutomationError(
                "El diálogo de selección múltiple de SAP no se cerró "
                "tras confirmar."
            ) from error

        self.page.locator("body").click()

        self.page.wait_for_timeout(300)

    def _find_take_button(self, dialog):
        """Busca el botón Tomar (F8) utilizando varios selectores alternativos."""

        logger.info(
            "Buscando botón 'Tomar (F8)'"
        )

        attempts = [
            lambda: dialog.get_by_role(
                "button",
                name="Tomar (F8)",
            ).first,

            lambda: dialog.get_by_role(
                "button",
                name="Tomar",
            ).first,

            lambda: dialog.locator(
                "div[role='button'][title='Tomar (F8)']"
            ),

            lambda: dialog.locator(
                "div[role='button'][title='Tomar']"
            ),

            lambda: dialog.get_by_title(
                "Tomar (F8)"
            ),
        ]

        for attempt_number, attempt in enumerate(
            attempts,
            start=1,
        ):
            try:
                button = attempt()

                button.wait_for(
                    state="visible",
                    timeout=3_000,
                )

                logger.info(
                    f"Botón Tomar localizado | intento={attempt_number}"
                )

                return button

            except Exception:
                logger.warning(
                    f"Intento {attempt_number} falló buscando botón Tomar"
                )

        logger.error(
            "No se encontró el botón Tomar"
        )

        raise SAPAutomationError(
            "No se encontró el botón 'Tomar (F8)' en el diálogo de "
            "selección múltiple de SAP."
        )

    # -----------------------------------------------------------------------
    # EJECUCIÓN DE LA CONSULTA (F8)
    # -----------------------------------------------------------------------

    def _execute_query(self) -> tuple[ExecutionResult, str]:
        """
        Ejecuta la consulta mediante F8 y espera el primer resultado
        disponible entre los estados definidos.
        """

        logger.info(
            "Ejecutando consulta SAP"
        )

        try:
            self.page.keyboard.press("F8")

            result, message = self._poll_execution_result()

            if result is ExecutionResult.ITEMS_FOUND:
                self._confirm_ok_dialog()

                self.page.wait_for_load_state(
                    "networkidle"
                )

                logger.info(
                    "Consulta ejecutada correctamente"
                )

            return result, message

        except SAPRPAError:
            raise

        except Exception as error:
            logger.exception(
                f"Error ejecutando consulta SAP: {error}"
            )
            raise

    def _confirm_ok_dialog(self):
        """
        Hace clic en el botón OK del diálogo, si está visible.

        Es un paso tolerante: si el botón no aparece porque SAP ya cerró
        el diálogo automáticamente, el flujo continúa.
        """

        try:
            ok_button = self.page.get_by_role(
                "button",
                name="OK",
            )

            ok_button.wait_for(
                state="visible",
                timeout=5_000,
            )

            ok_button.hover()
            ok_button.click()

            logger.info(
                "Popup OK detectado y cerrado"
            )

        except PlaywrightTimeoutError:
            logger.info(
                "No apareció popup OK (se continúa igualmente)"
            )

    # -----------------------------------------------------------------------
    # DETECCIÓN DEL RESULTADO
    # -----------------------------------------------------------------------

    def _poll_execution_result(self) -> tuple[ExecutionResult, str]:
        """
        Sondea la página después de F8 hasta detectar el primer resultado.

        El orden de evaluación es:

            1. ERROR
            2. NO_ITEMS
            3. ITEMS_FOUND

        Raises:
            SAPResultNotDetectedError: si se agotan los intentos sin
                detectar ningún resultado.
        """

        logger.info(
            "Iniciando espera de resultado tras F8 | "
            f"max_attempts={self.max_result_attempts} | "
            f"interval_ms={self.result_poll_interval_ms}"
        )

        # Orden de verificación: primero el error, luego ausencia de
        # partidas y finalmente la confirmación de partidas encontradas.
        detectors = (
            (
                ExecutionResult.ERROR,
                self._detect_error,
            ),
            (
                ExecutionResult.NO_ITEMS,
                self._detect_no_items,
            ),
            (
                ExecutionResult.ITEMS_FOUND,
                self._detect_items_found,
            ),
        )

        for attempt_number in range(
            1,
            self.max_result_attempts + 1,
        ):
            for result, detector in detectors:
                try:
                    found, message = detector()

                    if found:
                        logger.info(
                            f"Resultado detectado: {result.value} | "
                            f"intento={attempt_number}/"
                            f"{self.max_result_attempts} | "
                            f"mensaje={message!r}"
                        )

                        return result, message

                except Exception as error:
                    logger.warning(
                        f"Error evaluando detector "
                        f"'{result.value}' | "
                        f"intento={attempt_number} | "
                        f"{error}"
                    )

            self.page.wait_for_timeout(
                self.result_poll_interval_ms
            )

        logger.error(
            "Se agotaron los intentos sin detectar un resultado | "
            f"max_attempts={self.max_result_attempts}"
        )

        raise SAPResultNotDetectedError(
            "No se pudo determinar el resultado de la consulta en SAP "
            "(ni error de validación, ni ausencia de partidas, ni "
            f"partidas encontradas) tras {self.max_result_attempts} "
            "intentos. Verifique manualmente el estado de la pantalla."
        )

    def _detect_error(self) -> tuple[bool, str]:
        """
        Detecta si el campo 'Sociedad' está marcado como inválido.

        SAP utiliza aria-invalid="true" para indicar que el valor
        diligenciado no es válido.
        """

        try:
            company_code_field = self.page.get_by_role(
                "textbox",
                name=self.config.company_code_field_name,
            )

            if company_code_field.count() == 0:
                return False, ""

            field = company_code_field.first

            aria_invalid = field.get_attribute(
                "aria-invalid"
            )

            if aria_invalid != "true":
                return False, ""

            message = self._extract_error_message(
                field
            )

            return True, message

        except Exception:
            logger.debug(
                "No fue posible evaluar el campo 'Sociedad' en este intento",
                exc_info=True,
            )

            return False, ""

    def _extract_error_message(self, field) -> str:
        """
        Intenta obtener una explicación legible del error de validación
        generado por SAP.
        """

        # 1. aria-describedby suele apuntar al elemento que contiene
        # el mensaje de validación.
        try:
            described_by = field.get_attribute(
                "aria-describedby"
            )

            if described_by:
                for candidate_id in described_by.split():
                    element = self.page.locator(
                        f"#{candidate_id}"
                    )

                    if element.count() > 0:
                        text = element.first.inner_text().strip()

                        if text:
                            logger.debug(
                                "Mensaje de error obtenido vía aria-describedby"
                            )
                            return text

        except Exception:
            logger.debug(
                "No se pudo leer aria-describedby",
                exc_info=True,
            )

        # 2. Mensajes de alerta presentes en la pantalla SAP.
        try:
            alert = self.page.locator(
                "[role='alert']"
            ).first

            if alert.count() > 0:
                text = alert.inner_text().strip()

                if text:
                    logger.debug(
                        "Mensaje de error obtenido vía [role='alert']"
                    )
                    return text

        except Exception:
            logger.debug(
                "No se pudo leer [role='alert']",
                exc_info=True,
            )

        # 3. Título o tooltip asociado al campo.
        try:
            title = field.get_attribute(
                "title"
            )

            if title:
                logger.debug(
                    "Mensaje de error obtenido vía title del campo"
                )
                return title

        except Exception:
            logger.debug(
                "No se pudo leer title del campo",
                exc_info=True,
            )

        # 4. Mensaje genérico.
        logger.debug(
            "No se encontró un mensaje de error específico; "
            "usando mensaje genérico"
        )

        return (
            "El campo 'Sociedad' es inválido. "
            "Verifique que el código ingresado exista "
            "y esté correctamente escrito."
        )

    def _detect_no_items(self) -> tuple[bool, str]:
        """
        Detecta si SAP muestra un diálogo indicando que no se encontraron
        partidas para los criterios seleccionados.
        """

        try:
            info_dialog = self.page.get_by_label(
                self.config.information_dialog_label
            )

            if info_dialog.count() == 0:
                return False, ""

            dialog_text = (
                info_dialog.first
                .inner_text()
                .strip()
            )

            if self.config.no_items_text in dialog_text:
                message = dialog_text or (
                    "No se encontraron partidas para "
                    "los criterios seleccionados."
                )

                return True, message

            return False, ""

        except Exception:
            logger.debug(
                "No fue posible evaluar el diálogo de información",
                exc_info=True,
            )

            return False, ""

    def _detect_items_found(self) -> tuple[bool, str]:
        """
        Detecta el texto generado por SAP cuando existen partidas.

        El patrón esperado es similar a:

            "Se visualizan ... <n>"
        """

        try:
            page_text = (
                self.page
                .locator("body")
                .inner_text()
            )

            match = re.compile(
                self.config.items_found_pattern,
                re.IGNORECASE,
            ).search(
                page_text
            )

            if match:
                return True, match.group(0).strip()

            return False, ""

        except Exception:
            logger.debug(
                "No fue posible evaluar el texto de la página",
                exc_info=True,
            )

            return False, ""

    # -----------------------------------------------------------------------
    # FILTRADO Y DESBLOQUEO DE DOCUMENTOS
    # -----------------------------------------------------------------------

    def _filter_and_unlock_documents(self):
        """
        Filtra el listado de partidas por número de documento y, sobre ese
        subconjunto, ejecuta el desbloqueo masivo de bloqueo de pago.
        """

        logger.info("Iniciando filtrado de documentos")

        try:
            document_column_header = self.page.get_by_role(
                "button", name="Nº doc."
            )
            document_column_header.wait_for(state="visible", timeout=10_000)
            document_column_header.click()
            logger.info("Columna 'Nº doc.' seleccionada")

            # Espera a que SAP termine su round-trip tras la selección de
            # columna antes de intentar abrir el menú de filtros.
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            self.page.wait_for_timeout(500)

            set_filter_button = self.page.get_by_role(
                "button", name="Fijar filtros (Control+Mayús+F2)"
            )
            set_filter_button.wait_for(state="visible", timeout=10_000)
            set_filter_button.click()
            logger.info("Menú 'Fijar filtros' abierto")

            document_numbers_text = "\n".join(self.document_numbers_to_unlock)
            self._paste_into_dialog(document_numbers_text, is_filter_dialog=True)

            logger.info("Filtrado de documentos completado")

            self._unlock_filtered_items()

        except SAPRPAError:
            raise

        except Exception as error:
            logger.exception(
                f"Error filtrando/desbloqueando documentos: {error}"
            )
            raise

    def _unlock_filtered_items(self):
        """
        Ejecuta la modificación en masa para desbloquear el pago de las
        partidas filtradas previamente.
        """

        logger.info("Iniciando desbloqueo masivo de partidas filtradas")

        try:
            self.page.locator("body").click()
            self.page.keyboard.press("F5")
            logger.info("Refresco (F5) enviado antes de la modificación en masa")

            mass_modify_button = self.page.get_by_role(
                "button", name="Modificación en masa (Control"
            )
            mass_modify_button.wait_for(state="visible", timeout=10_000)
            self.page.wait_for_timeout(500)
            mass_modify_button.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            logger.info("Modal de modificación en masa abierto")

            modify_modal_heading = self.page.get_by_role(
                "heading", name="Val.nuevos"
            )
            modify_modal_heading.wait_for(state="visible", timeout=10_000)
            self.page.wait_for_load_state("networkidle", timeout=10_000)

            payment_block_field = self.page.get_by_role(
                "textbox", name="Bloqueo de pago"
            )
            payment_block_field.wait_for(state="visible", timeout=10_000)
            payment_block_field.click()
            logger.info("Campo 'Bloqueo de pago' enfocado")

            payment_block_help_button = self.page.locator(
                "#ls-inputfieldhelpbutton"
            )
            payment_block_help_button.wait_for(state="visible", timeout=10_000)
            payment_block_help_button.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            logger.info("Ayuda de valores de 'Bloqueo de pago' abierta")

            payment_block_options_heading = self.page.get_by_role(
                "heading", name="Clave para bloqueo de pago (1)"
            )
            payment_block_options_heading.wait_for(state="visible", timeout=10_000)
            payment_block_options_heading.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)

            authorized_payment_option = self.page.get_by_text(
                "Autorizado el pago"
            )
            authorized_payment_option.wait_for(state="visible", timeout=10_000)
            authorized_payment_option.click()
            logger.info("Opción 'Autorizado el pago' seleccionada")

            ok_button = self.page.get_by_role(
                "button", name="OK  Resaltado"
            )
            ok_button.wait_for(state="visible", timeout=10_000)
            ok_button.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            logger.info("Selección de valor confirmada")

            execute_changes_button = self.page.get_by_role(
                "button", name="Ejecutar modificaciones"
            )
            execute_changes_button.wait_for(state="visible", timeout=10_000)
            execute_changes_button.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            logger.info("Modificaciones en masa ejecutadas")

            self._confirm_ok_dialog()
            self._close_no_documents_modified_dialog()

            logger.info("Desbloqueo masivo de partidas finalizado correctamente")

        except SAPRPAError:
            raise

        except Exception as error:
            logger.exception(
                f"Error desbloqueando partidas filtradas: {error}"
            )
            raise

    def _close_no_documents_modified_dialog(self):
        """
        Cierra el popup alterno que SAP muestra cuando no todos los
        documentos pudieron modificarse. Es un paso tolerante: si el popup
        no aparece, el flujo continúa sin error.
        """

        try:
            no_documents_modified_text = self.page.get_by_text(
                "No pudieron modificarse todos"
            )
            no_documents_modified_text.wait_for(state="visible", timeout=10_000)

            logger.info(
                "Popup 'No pudieron modificarse todos' detectado; cerrando"
            )

            # OJO: Locator.wait_for() retorna None en la API síncrona de
            # Playwright, por lo que NO se puede encadenar .click() después.
            # Hay que separar la espera del clic en dos pasos.
            continue_button = self.page.get_by_role(
                "button", name="Continuar (Entrada)"
            )
            continue_button.wait_for(state="visible", timeout=10_000)
            continue_button.click()

            # IMPORTANTE: no hacer click en 'body' aquí. Un click sobre el
            # <body> genérico saca el foco del contenedor interno de SAP y
            # rompe el reconocimiento de atajos de teclado más adelante
            # (p. ej. Shift+F4 en la descarga deja de abrir el popup de
            # exportación). Basta con esperar a que SAP termine de procesar
            # el cierre del popup para que el foco regrese de forma natural
            # al grid/toolbar.
            self.page.wait_for_load_state("networkidle", timeout=10_000)

            logger.info("Popup de documentos no modificados cerrado")

        except Exception:
            logger.info(
                "No apareció el popup de documentos no modificados "
                "(se continúa igualmente)"
            )

    # -----------------------------------------------------------------------
    # DESCARGA
    # -----------------------------------------------------------------------

    def _download_report(self, report_name: str) -> Path:
        """Descarga el reporte Excel generado desde FBL5N."""

        logger.info(
            f"Iniciando descarga | report_name={report_name}"
        )

        # SAP necesita un momento para terminar de procesar el desbloqueo
        # masivo (y el posible cierre del popup de "no modificados") antes
        # de que la exportación responda de forma confiable.
        logger.info(
            "Esperando a que SAP responda tras el desbloqueo masivo "
            "antes de iniciar la descarga"
        )
        self.page.wait_for_load_state("networkidle", timeout=10_000)
        self.page.wait_for_timeout(1_000)

        output_path = (
            self.output_dir /
            f"{report_name}.xlsx"
        )

        try:
            self.page.keyboard.press(
                "Shift+F4"
            )

            logger.info(
                "Atajo Shift+F4 ejecutado"
            )

            filename_field = self.page.locator(
                "[name='InputField']"
            )

            try:
                filename_field.wait_for(
                    state="visible",
                    timeout=10_000,
                )

            except PlaywrightTimeoutError as error:
                logger.exception(
                    "No apareció popup de exportación"
                )

                raise SAPAutomationError(
                    "No apareció el campo de nombre de fichero tras "
                    "Shift+F4 al intentar exportar el reporte."
                ) from error

            filename_field.click(
                click_count=3
            )

            filename_field.fill(
                report_name
            )

            logger.info(
                "Nombre del reporte diligenciado"
            )

            # SAP puede resolver la exportación de dos formas distintas:
            #
            #   Flujo A: tras el Enter aparece un segundo popup pidiendo
            #            la ruta del archivo ("Introducir el nombre del
            #            fichero").
            #   Flujo B: tras el Enter la descarga se dispara directamente,
            #            sin popup adicional.
            #
            # Es crítico envolver ESE MISMO Enter dentro de
            # expect_download() desde el principio: si la descarga ocurre
            # justo ahí (flujo B) y no estamos escuchando el evento en ese
            # instante, Playwright lo pierde para siempre y cualquier
            # expect_download() posterior espera indefinidamente una
            # descarga que ya ocurrió y no va a volver a dispararse. Este
            # era el motivo por el que el flujo B nunca funcionaba, y por
            # el que el flujo A fallaba cuando SAP tardaba un poco más de
            # la cuenta en mostrar el segundo popup.
            #
            # Los timeouts se dejan generosos (10s / 15s) porque en el
            # entorno real, justo después del desbloqueo masivo, SAP puede
            # tardar bastante más que en pruebas locales en responder.
            download = None

            try:
                with self.page.expect_download(
                    timeout=5_000
                ) as download_info:
                    self.page.keyboard.press("Enter")

                download = download_info.value

                logger.info(
                    "Descarga disparada directamente (flujo B)"
                )

            except PlaywrightTimeoutError:
                logger.info(
                    "No se disparó descarga inmediata; "
                    "esperando popup de nombre de archivo (flujo A)"
                )

                # El título y el nombre del campo de este popup varían
                # entre versiones/configuraciones de SAP (se ha observado
                # tanto "Introducir el nombre del fichero" con campo
                # 'Fichero' como "Introducir el nombre del archivo que se
                # debe guardar" con campo 'Nombre de archivo'). Se acota
                # el diálogo por texto parcial para cubrir ambas variantes.
                save_filename_dialog = self.page.get_by_role(
                    "dialog"
                ).filter(
                    has_text="Introducir el nombre del"
                )

                try:
                    save_filename_dialog.first.wait_for(
                        state="visible",
                        timeout=15_000,
                    )

                except PlaywrightTimeoutError as error:
                    logger.exception(
                        "No se disparó la descarga ni apareció "
                        "el popup de nombre de archivo esperado"
                    )

                    raise SAPReportDownloadError(
                        "Tras confirmar el nombre de archivo, SAP no "
                        "disparó la descarga ni mostró el popup de "
                        "nombre de archivo esperado."
                    ) from error

                dialog = save_filename_dialog.first

                # El campo suele venir prellenado por SAP con el nombre
                # correcto (el mismo que se diligenció en filename_field).
                # Se confirma explícitamente el valor por robustez, sin
                # depender de que el prellenado sea siempre correcto.
                filename_input = dialog.get_by_role(
                    "textbox", name="Nombre de archivo"
                ).or_(
                    dialog.get_by_role("textbox", name="Fichero")
                ).or_(
                    dialog.locator("input[type='text']").first
                )

                try:
                    filename_input.first.wait_for(
                        state="visible",
                        timeout=5_000,
                    )

                    filename_input.first.click(
                        click_count=3
                    )

                    filename_input.first.fill(
                        f"{report_name}.xlsx"
                    )

                    logger.info(
                        "Nombre de archivo confirmado en el popup"
                    )

                except PlaywrightTimeoutError:
                    logger.warning(
                        "No se encontró un campo de nombre editable en el "
                        "popup; se usa el valor prellenado por SAP"
                    )

                ok_button = dialog.get_by_role(
                    "button", name="OK"
                )

                ok_button.wait_for(
                    state="visible",
                    timeout=5_000,
                )

                with self.page.expect_download(
                    timeout=30_000
                ) as download_info:
                    ok_button.click()

                download = download_info.value

                logger.info(
                    "Descarga disparada vía popup de nombre de archivo (flujo A)"
                )

            logger.info(
                "Descarga capturada correctamente"
            )

            download.save_as(
                output_path
            )

            logger.info(
                f"Archivo guardado | path={output_path}"
            )

            if (
                output_path.exists()
                and output_path.stat().st_size > 0
            ):
                logger.info(
                    f"Reporte descargado correctamente | "
                    f"path={output_path}"
                )

                return output_path

            logger.error(
                "Archivo descargado vacío o inexistente"
            )

            raise SAPReportDownloadError(
                f"La descarga del reporte '{report_name}' no se "
                "completó correctamente (archivo vacío o inexistente)."
            )

        except SAPRPAError:
            raise

        except Exception as error:
            logger.exception(
                f"Error descargando reporte: {error}"
            )
            raise