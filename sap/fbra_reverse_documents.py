from __future__ import annotations

import logging

import pandas as pd
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect
from .sap_exceptions import (
    SAPAutomationError,
    SAPRPAError,
    SAPValidationError,
)
from .sap_config import FBRAConfig
from .sap_error_recorder import ensure_error_columns, record_error


logger = logging.getLogger("estado_cuenta")


class FBRAReverseDocuments:
    """
    Anula la compensación de documentos mediante la transacción FBRA.

    Recibe un DataFrame de partidas compensadas previamente obtenido y
    procesado. Para cada registro utiliza su documento de compensación
    para realizar la anulación en SAP.

    Esta clase únicamente se encarga de la automatización de FBRA.
    No contiene lógica de consulta, matching ni normalización del
    DataFrame.
    """


    def __init__(
        self,
        page: Page,
        config: FBRAConfig,
        df_errores: pd.DataFrame | None = None,
    ):
        self.page = page
        self.config = config
        self.df_errores = ensure_error_columns(df_errores)

        logger.info(
            "Inicializando FBRAReverseDocuments | "
        )

    # ------------------------------------------------------------------
    # PUNTO DE ENTRADA
    # ------------------------------------------------------------------

    def process(self,page:Page  ,clearing_document:str, period:str) -> bool:
        """
        Ejecuta el proceso de anulación de compensaciones en FBRA.
        """

        logger.info(
            "Iniciando proceso de anulación de compensación en FBRA "
            "para documento %s y periodo %s",
            clearing_document,
            period,
        )
        try:
            self.page = page
            self._navigate_to_fbra()
            success = self._reverse_document(clearing_document,period)
            return success
        except:
            logger.error(
            "Error proceso de anulación de compensación en FBRA "
            "para documento %s y periodo %s",
            clearing_document,
            period,
            )
            raise 


    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_fbra(self) -> None:
        """
        Ingresa a la transacción FBRA.
        """

        logger.info("Navegando a transacción FBRA")

        try:
            ok_code_field = self.page.locator(
                "input[id='ToolbarOkCode']"
            )

            ok_code_field.wait_for(
                state="visible",
                timeout=30_000,
            )

            ok_code_field.fill(
                f"/n{self.config.transaction}"
            )

            self.page.keyboard.press("Enter")

            self.page.wait_for_load_state(
                "networkidle"
            )

            logger.info(
                "Transacción FBRA cargada correctamente"
            )

        except PlaywrightTimeoutError:
            logger.exception(
                "Timeout navegando a la transacción FBRA"
            )
            raise

        except Exception as error:
            logger.exception(
                f"Error navegando a FBRA: {error}"
            )
            raise SAPAutomationError(
                f"No fue posible navegar a FBRA: {error}"
            ) from error

    # ------------------------------------------------------------------
    # ANULACIÓN
    # ------------------------------------------------------------------

    def _reverse_document(
        self,
        clearing_document: str,
        period: str,
    ) -> bool:
        """
        Anula una compensación utilizando su documento de compensación.
        ...
        """

        if not clearing_document:
            raise SAPValidationError(
                "El documento de compensación está vacío."
            )

        logger.info(
            "Iniciando anulación | "
            f"clearing_document={clearing_document} | "
            f"accounting_period={period}"
        )

        # --------------------------------------------------------------
        # DILIGENCIAMIENTO DE DATOS
        # --------------------------------------------------------------

        self._fill_field(
            "Doc.compensación",
            "Doc.compensación",
            clearing_document,
        )

        self._fill_field(
            "Sociedad",
            "Sociedad",
            self.config.company_code,
        )

        self._fill_field(
            "Ejercicio",
            "Ejercicio",
            period,
        )

        # Fuerza el evento onChange/blur del último campo para que SAP
        # revalide la pantalla y habilite el botón de anulación.
        self.page.keyboard.press("Tab")

        # --------------------------------------------------------------
        # ANULAR COMPENSACIÓN
        # --------------------------------------------------------------

        logger.info(
            "Disparando 'Anular compensación' vía atajo Ctrl+S | "
            f"documento={clearing_document}"
        )

        try:
            self.page.keyboard.press("Control+s")
        except Exception as error:
            logger.exception(
                f"Error al disparar el atajo Ctrl+S para anular compensación: {error}"
            )
            raise SAPAutomationError(
                "No fue posible disparar 'Anular compensación' vía Ctrl+S."
            ) from error

        # --------------------------------------------------------------
        # VERIFICAR RESPUESTA INICIAL
        # --------------------------------------------------------------

        information_dialog = self.page.get_by_role(
            "heading",
            name="Diálogo de información",
        )

        blocked_message = information_dialog.get_by_text(
            "está bloqueado por un usuario",
        )

        invalid_document_message = information_dialog.get_by_text(
            "no es doc.compensación",
        )

        confirmation_heading = self.page.get_by_role(
            "heading",
            name="Anulación del doc.de",
        )

        first_response = (
            blocked_message
            .or_(invalid_document_message)
            .or_(confirmation_heading)
        )

        try:
            first_response.wait_for(
                state="visible",
                timeout=5_000,
            )

        except PlaywrightTimeoutError:
            logger.error(
                "No se obtuvo una respuesta de SAP después de "
                "seleccionar 'Anular compensación' | "
                f"documento={clearing_document}"
            )
            raise

        # --------------------------------------------------------------
        # DOCUMENTO BLOQUEADO
        # --------------------------------------------------------------

        if blocked_message.is_visible():
            error_message = information_dialog.inner_text()

            logger.error(
                f"Documento {clearing_document} bloqueado | "
                f"mensaje SAP: {error_message}"
            )

            raise Exception(
                f"Documento {clearing_document} bloqueado | "
                f"mensaje SAP: {error_message}"
            )

        # --------------------------------------------------------------
        # DOCUMENTO NO VÁLIDO PARA COMPENSACIÓN
        # --------------------------------------------------------------

        if invalid_document_message.is_visible():
            error_message = information_dialog.inner_text()

            logger.error(
                f"Documento {clearing_document} no válido para compensación | "
                f"mensaje SAP: {error_message}"
            )

            raise Exception(
                f"Documento {clearing_document} no es un documento de "
                f"compensación | mensaje SAP: {error_message}"
            )

        # --------------------------------------------------------------
        # CONFIRMACIÓN DE ANULACIÓN
        # --------------------------------------------------------------

        try:
            self.page.get_by_role(
                "button",
                name="Sí",
                exact=True,
            ).click(timeout=5_000)

            logger.info(
                "Anulación confirmada | "
                f"documento={clearing_document}"
            )

        except PlaywrightTimeoutError:
            logger.error(
                "No fue posible encontrar el botón 'Sí' para confirmar "
                f"la anulación | documento={clearing_document}"
            )
            raise

        # --------------------------------------------------------------
        # DATOS DE ANULACIÓN + RESULTADO FINAL
        # --------------------------------------------------------------

        reversal_heading = self.page.get_by_role(
            "heading",
            name="Datos anul.",
        )

        try:
            reversal_heading.wait_for(
                state="visible",
                timeout=5_000,
            )

            logger.info(
                "Pantalla de datos de anulación detectada | "
                f"documento={clearing_document}"
            )

            self.page.get_by_role(
                "textbox",
                name="Motiv.anulación Necesarios",
            ).fill(
                self.config.reversal_reason,
                timeout=5_000,
            )

            self.page.get_by_role(
                "button",
                name="Continuar (Entrada)",
                exact=True,
            ).click(timeout=5_000)

            logger.info(
                "Botón 'Continuar (Entrada)' presionado, verificando resultado | "
                f"documento={clearing_document}"
            )

            # ----------------------------------------------------------
            # VERIFICAR RESULTADO FINAL: autorización faltante vs. éxito
            # ----------------------------------------------------------

            authorization_error = self.page.get_by_text(
                "Falta autorización para transacción FB08",
            )

            success_indicator = self.page.locator(
                "text=/anulado/i"
            )  # TODO: ajustar al mensaje real de éxito cuando se confirme

            final_result = authorization_error.or_(success_indicator)

            final_result.wait_for(
                state="visible",
                timeout=10_000,
            )

            if authorization_error.is_visible():
                error_message = authorization_error.inner_text()

                logger.error(
                    f"Documento {clearing_document} no anulado — "
                    f"falta autorización para FB08 | mensaje SAP: {error_message}"
                )

                raise SAPAutomationError(
                    f"El usuario técnico no tiene autorización para FB08. "
                    f"Documento {clearing_document} no fue anulado."
                )

            logger.info(
                "Anulación completada y verificada | "
                f"documento={clearing_document}"
            )

        except PlaywrightTimeoutError:
            logger.error(
                "No apareció la pantalla de datos de anulación | "
                f"documento={clearing_document}"
            )
            return False

        return True



    
    def _fill_field(self, label: str, role_name: str, value: str, exact: bool = False) -> None:
        """
        Localiza un textbox por su rol/nombre, espera visibilidad, lo llena
        y verifica que SAP no lo haya rechazado.

        Lanza:
            SAPAutomationError: si el campo no se pudo localizar / llenar
                (timeout, elemento no encontrado, etc. — fallo técnico,
                no de negocio).
            SAPValidationError: si SAP marcó el campo como inválido tras
                llenarlo (aria-invalid="true"), típicamente porque el
                valor ingresado no existe o no es válido para SAP.
        """
        logger.info(f"Ingresando {label.lower()}: {value}")

        try:
            field_input = self.page.get_by_role("textbox", name=role_name, exact=exact)
            field_input.wait_for(state="visible", timeout=10_000)
            field_input.fill(value)

        except PlaywrightTimeoutError as e:
            logger.exception(f"No se pudo localizar/llenar el campo '{label}': {e}")
            raise SAPAutomationError(
                f"No se pudo llenar el campo '{label}' (valor: '{value}'): "
                "el elemento no apareció en pantalla dentro del tiempo esperado."
            ) from e

        except Exception as e:
            logger.exception(f"Error inesperado llenando el campo '{label}': {e}")
            raise SAPAutomationError(
                f"Error inesperado al intentar llenar el campo '{label}' "
                f"(valor: '{value}')."
            ) from e

        # Verifica que SAP no haya rechazado el valor ingresado.
        try:
            is_invalid = field_input.get_attribute("aria-invalid") == "true"
        except Exception:
            # Si no se puede leer el atributo, no bloqueamos el flujo por esto;
            # cualquier problema real se detectará más adelante en _execute().
            is_invalid = False

        if is_invalid:
            logger.error(f"SAP rechazó el valor ingresado en '{label}': '{value}'")
            raise SAPValidationError(
                f"SAP rechazó el valor ingresado en el campo '{label}': '{value}'. "
                "Verifica que el dato exista y sea válido."
            )