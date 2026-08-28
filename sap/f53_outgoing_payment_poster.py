"""
f53_outgoing_payment.py

Automatización de la transacción F-53 (pago saliente / compensación de
partidas abiertas) en SAP WebGUI mediante Playwright.
"""

from datetime import date as date_type
import logging
import re
from typing import Final
import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Page

from .sap_config import F53Config
from .sap_exceptions import (
    SAPRPAError,
    SAPAutomationError,
    SAPNoItemsFoundError,
    SAPValidationError,
)
from .sap_error_recorder import ensure_error_columns, record_error
from models.models import AccountClearingItem

logger = logging.getLogger(__name__)


class F53OutgoingPayment:
    """
    Crea el pago manual de las devoluciones online desde SAP mediante
    la transacción F-53.

    Reutiliza una página de navegador ya autenticada (sesión compartida).
    """

    # Definición declarativa de los campos de cabecera a llenar.
    # (etiqueta_log, role_name, exact, attr_path)
    # attr_path puede ser un atributo de instancia ("text", "date") o de
    # self.config ("config.document_type").
    _HEADER_FIELDS = [
        ("Fecha de documento", "Fecha documento Necesarios", False, "date"),
        ("Fecha de contabilización", "Fecha contab. Necesarios", False, "date"),
        ("Clase de documento", "Clase doc. Necesarios", False, "config.document_type"),
        ("Sociedad", "Sociedad Necesarios", False, "config.company_code"),
        ("Moneda", "Moneda/T/C Necesarios", False, "config.currency"),
        ("Referencia", "Referencia", False, "text"),
        ("Texto documento", "Txt.cab.doc.", False, "text"),
        ("Texto contabilización", "Texto compens.", False, "text"),
        ("Cuenta de salida", "Cuenta Necesarios", False, "config.outgoing_bank_account"),
        ("Importe", "Importe", True, "amount"),
        ("Texto bancario", "Texto", True, "text"),
    ]

    def __init__(
        self,
        page: Page,
        items: list[AccountClearingItem],
        date: date_type,
        text: str,
        amount: str,
        config: F53Config | None = None,
        df_errores: pd.DataFrame | None = None,
    ):
        self.page = page
        self.items = items
        self.date = date
        self.text = text
        self.amount = amount
        self.config = config or F53Config()
        self.df_errores = ensure_error_columns(df_errores)
        self._last_document_dialog: str | None = None

        logger.info(
            "Inicializando F53OutgoingPayment | "
            f"page={self.page} | "
            f"date={self.date} | "
            f"items={self.items} | "
            f"text={self.text} | "
            f"amount={self.amount}"
        )

    # ------------------------------------------------------------------
    # PUNTO DE ENTRADA
    # ------------------------------------------------------------------

    def process(self) -> None:
        """
        Ejecuta el flujo completo: navega a F-53, llena los campos de
        cabecera y procesa/contabiliza las partidas abiertas.

        Deja propagar las excepciones de `sap_exceptions` tal cual
        (SAPValidationError, SAPNoItemsFoundError, SAPAutomationError, etc.)
        para que el llamador pueda diferenciarlas. Cualquier otro error no
        previsto se envuelve en SAPAutomationError con un mensaje legible.
        """
        logger.info("Iniciando contabilización de pago saliente F-53")

        try:
            self._navigate_to_f53()
            self._fill_header_fields()
            self._execute()

            logger.info("Pago contabilizado correctamente")

        except PlaywrightTimeoutError as e:
            logger.exception(f"Timeout durante la contabilización del pago: {e}")
            error = SAPAutomationError(
                f"Timeout durante la contabilización del pago: {e}"
            )
            record_error(self.df_errores, self.__class__.__name__, "process", error)
            raise error from e

        except SAPRPAError as error:
            # SAPValidationError, SAPNoItemsFoundError, SAPAutomationError, etc.
            # ya vienen con mensaje legible construido en el punto donde ocurrieron.
            logger.exception("Fallo de automatización SAP durante process()")
            record_error(self.df_errores, self.__class__.__name__, "process", error)
            raise

        except Exception as e:
            logger.exception(f"Error inesperado contabilizando el pago: {e}")
            error = SAPAutomationError(f"Error inesperado: {e}")
            record_error(self.df_errores, self.__class__.__name__, "process", error)
            raise error from e

    def get_errors(self) -> pd.DataFrame:
        """Devuelve los errores visibles registrados durante el proceso."""
        return self.df_errores

    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_f53(self) -> None:
        """Ingresa a la transacción F-53."""
        logger.info("Navegando a transacción F-53")

        try:
            ok_field = self.page.locator("input[id='ToolbarOkCode']")
            ok_field.wait_for(state="visible", timeout=30_000)

            ok_field.click()
            ok_field.fill(f"/n{self.config.transaction}")

            self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("networkidle")

            logger.info("Transacción F-53 cargada correctamente")

        except Exception as e:
            logger.exception(f"Error navegando a F-53: {e}")
            raise

    # ------------------------------------------------------------------
    # LLENADO DE CAMPOS DE CABECERA
    # ------------------------------------------------------------------

    def _resolve_value(self, attr_path: str) -> str:
        """
        Resuelve el valor a partir de un path tipo 'config.document_type'
        o 'text', buscando primero en self.config y luego en self.
        """
        obj = self
        for part in attr_path.split("."):
            obj = getattr(obj, part)

        # Formatea fechas automáticamente
        if isinstance(obj, date_type):
            return obj.strftime("%d.%m.%Y")

        # Playwright requiere texto nativo; esto también normaliza valores
        # numéricos provenientes de pandas/numpy.
        return str(obj)

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

    def _fill_header_fields(self) -> None:
        """
        Llena los campos de cabecera requeridos para contabilizar el pago
        (fechas, clase de documento, sociedad, moneda, textos, cuenta de
        salida e importe).

        Lanza:
            ValueError: si no hay items (cuenta + documentos) para procesar,
                o si alguna cuenta no tiene documentos asociados.
            SAPValidationError: si algún campo fue rechazado por SAP.
            SAPAutomationError: si algún campo no pudo llenarse por un
                problema técnico de la interfaz.
        """
        logger.info("Iniciando llenado de campos de cabecera")

        if not self.items:
            logger.error("La lista de items (cuenta + documentos) está vacía")
            raise ValueError("La lista de items (cuenta + documentos) está vacía.")

        for item in self.items:
            if not item.docs:
                logger.error(f"La cuenta {item.account} no tiene documentos asociados")
                raise ValueError(
                    f"La cuenta {item.account} no tiene documentos asociados."
                )

        for label, role_name, exact, attr_path in self._HEADER_FIELDS:
            value = self._resolve_value(attr_path)
            self._fill_field(label, role_name, value, exact=exact)

        logger.info("Campos de cabecera diligenciados correctamente")

    # ------------------------------------------------------------------
    # PROCESAMIENTO Y CONTABILIZACIÓN DE PARTIDAS ABIERTAS
    # ------------------------------------------------------------------

    def _execute(self) -> bool:
        """
        Ingresa la cuenta y clase de cuenta, selecciona el tipo de partida
        ('Nº documento'), procesa ('Tratar PAs') los documentos abiertos de
        cada cuenta a compensar y finalmente contabiliza la compensación.

        Returns:
            True si la compensación se contabilizó correctamente.

        Lanza:
            SAPNoItemsFoundError: si alguna cuenta no tiene partidas
                abiertas disponibles para compensar.
            SAPAutomationError: si algún paso técnico de la interfaz falla.
        """
        first_item = self.items[0]
        several_accounts = len(self.items) > 1

        self._fill_field(
            "Cuenta para ingresar partidas abiertas", "Cuenta",
            first_item.account, exact=True,
        )
        self._fill_field(
            "Clase de cuenta", "Clase de cuenta",
            self._resolve_value("config.account_type"), exact=True,
        )

        normal_items_checkbox = self.page.get_by_role(
            "checkbox", name=self.config.normal_items_label
        )
        normal_items_checkbox.check()

        document_number_radio = self.page.get_by_role(
            "radio", name=self.config.document_number_label
        )
        document_number_radio.check()

        self.page.wait_for_timeout(500)

        process_items_button = self.page.get_by_role(
            "button", name=self.config.process_items_label
        )
        process_items_button.click()

        self._fill_open_item_documents(first_item.docs, is_last=not several_accounts)
        if self._last_document_dialog != "selected_items":
            raise SAPAutomationError(
                "No se detectó el diálogo de confirmación de partidas seleccionadas."
            )
        if several_accounts:
            remaining_items = self.items[1:]

            for index, item in enumerate(remaining_items):
                is_last = index == len(remaining_items) - 1

                self._fill_field("Otra cuenta", "Cuenta", item.account, exact=True)
                self._fill_field(
                    "Clase de cuenta", "Clase de cuenta Necesarios",
                    self._resolve_value("config.account_type"), exact=True,
                )

                document_number_radio = self.page.get_by_role(
                    "radio", name=self.config.document_number_label
                )
                document_number_radio.check()

                process_items_button = self.page.get_by_role(
                    "button", name=self.config.process_items_label
                )
                process_items_button.click()

                self._fill_open_item_documents(item.docs, is_last=is_last)
                expected_dialog = "added_items" if is_last else "selected_items"
                dialog_closed = self._last_document_dialog != expected_dialog

                if dialog_closed:
                    raise SAPAutomationError(
                        "No se detectó el diálogo de confirmación de partidas seleccionadas."
                    )

        # ── Contabilizar ─────────────────────────────────────────────


        post_button = self.page.get_by_role(
            "button", name=self.config.post_button_label
        )
  
        post_button.click(timeout=10_000)
        self.page.wait_for_load_state("networkidle")
        self.page.pause()
        if not self._clearing_posted():
            raise SAPAutomationError(
                "SAP no confirmó la contabilización de la compensación."
            )

        return True

    def _fill_open_item_documents(self, docs: list[str], is_last: bool = False) -> bool:
        """
        Ingresa números de documento en los campos 'De' de la tabla de
        partidas abiertas.

        SAP renderiza un número limitado de filas según el zoom del
        navegador. Cuando se llenan todos los campos visibles se presiona
        Enter para que SAP procese el lote y habilite más campos. El ciclo
        se repite hasta agotar la lista completa de documentos.

        Nota sobre índices: SAP incluye un campo de cabecera 'De' que ocupa
        nth(0), por lo que los campos editables comienzan en nth(1).
        `field_index` representa siempre el índice del campo editable actual.

        Args:
            docs: Lista de números de documento a ingresar.
            is_last: True si esta es la última cuenta a procesar (se
                presiona 'Tratar PAs' al finalizar); False si hay más
                cuentas pendientes (se presiona 'Otra cuenta').

        Returns:
            True si todos los documentos fueron ingresados correctamente.

        Lanza:
            SAPNoItemsFoundError: si SAP reporta que no hay partidas
                abiertas para compensar.
            SAPAutomationError: si un campo no responde en el tiempo
                esperado o ocurre cualquier otro error técnico inesperado.
        """
        logger.info("Iniciando llenado de documentos | total_docs=%d", len(docs))

        try:
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(1_500)

            # Cuenta campos editables disponibles (se resta 1 por la cabecera).
            # Hasta 4 reintentos de 500 ms si SAP aún no los ha renderizado.
            total_fields = self.page.get_by_role("textbox", name="De").count() - 1
            for _ in range(4):
                if total_fields <= 0:
                    self.page.wait_for_timeout(500)
                    total_fields = self.page.get_by_role("textbox", name="De").count() - 1
                else:
                    break

            logger.info("Campos 'De' editables renderizados por SAP: %d", total_fields)

            if total_fields <= 0:
                raise SAPAutomationError(
                    "No se encontraron campos 'De' disponibles para ingresar "
                    "números de documento en F-53."
                )

            field_index = 1  # nth(1) = primer campo editable (nth(0) es la cabecera)
            total_docs = len(docs)

            for i in range(total_docs):
                self.page.wait_for_timeout(300)

                field = self.page.get_by_role("textbox", name="De").nth(field_index)

                try:
                    field.wait_for(state="visible", timeout=500)
                    for _ in range(4):
                        if not field.is_visible():
                            self.page.wait_for_timeout(500)
                        else:
                            break

                    field.fill(docs[i])
                    logger.info(
                        "Documento ingresado | pos=%d | doc=%s | progreso=%d/%d",
                        field_index, docs[i], i + 1, total_docs,
                    )

                except PlaywrightTimeoutError as e:
                    logger.exception(
                        "Timeout esperando campo 'De' en posición %d para documento %s",
                        field_index, docs[i],
                    )
                    raise SAPAutomationError(
                        f"Tiempo de espera agotado esperando el campo para "
                        f"ingresar el documento '{docs[i]}'."
                    ) from e

                # ── Último documento: cerrar el formulario ────────────
                if i == total_docs - 1:
                    if is_last:
                        logger.info("Último documento ingresado — presionando 'Tratar PAs' para finalizar")
                        self.page.get_by_role(
                            "button", name=self.config.process_items_label
                        ).click(timeout=2_000)
                    else:
                        logger.info("Último documento ingresado — presionando 'Otra cuenta' para finalizar")
                        self.page.get_by_role(
                            "button", name=self.config.other_account_label
                        ).click(timeout=2_000)

                    self.page.wait_for_load_state("networkidle")

                    dialog_result = self._resolve_document_dialog()
                    self._last_document_dialog = dialog_result
                    if dialog_result == "no_open_items":
                        raise SAPNoItemsFoundError(
                            "SAP reportó que no hay partidas abiertas para compensar."
                        )

                # ── Lote completo: enviar a SAP y preparar siguiente lote ─
                elif field_index == total_fields:
                    logger.info("Lote completo (%d campos) — enviando a SAP con Enter", total_fields)
                    self.page.keyboard.press("Enter")
                    self.page.wait_for_load_state("networkidle")
                    self.page.wait_for_timeout(500)

                    dialog_result = self._resolve_document_dialog()
                    self._last_document_dialog = dialog_result
                    if dialog_result == "no_open_items":
                        raise SAPNoItemsFoundError(
                            "SAP reportó que no hay partidas abiertas para compensar."
                        )

                    if dialog_result == "batch_recorded":
                        # SAP limpió los campos; reinicia el índice para el siguiente lote.
                        logger.info("Lote grabado — reiniciando índice de campos para el siguiente lote")
                        field_index = 1
                        continue

                # ── Avanzar al siguiente campo dentro del lote actual ──
                else:
                    field_index += 1

            logger.info(
                "Llenado de documentos completado exitosamente | total_ingresados=%d",
                total_docs,
            )
            return True

        except SAPRPAError:
            # SAPNoItemsFoundError / SAPAutomationError ya vienen con mensaje
            # legible construido en el punto donde ocurrieron.
            raise

        except PlaywrightTimeoutError as e:
            logger.exception("Timeout durante el llenado de documentos")
            raise SAPAutomationError("Timeout durante el llenado de documentos.") from e

        except Exception as e:
            logger.exception("Error inesperado durante el llenado de documentos")
            raise SAPAutomationError("Error inesperado durante el llenado de documentos.") from e

    # ------------------------------------------------------------------
    # DETECCIÓN DE DIÁLOGOS SAP
    # ------------------------------------------------------------------

    def _sap_object_error_text(self) -> str | None:
        """Devuelve el texto completo del popup de objeto bloqueado."""
        error_marker = "E: El objeto solicitado"
        selectors = (
            "span#promptDialogTextView",
            "[id^='webguiPopupWindow'][id$='-contentsection']",
        )

        for selector in selectors:
            elements = self.page.locator(selector)
            for index in range(elements.count()):
                element = elements.nth(index)
                if not element.is_visible():
                    continue

                text = (element.inner_text() or "").strip()
                if error_marker.casefold() in text.casefold():
                    return text

        return None

    def _resolve_document_dialog(self) -> str | None:
        """Resuelve el primer popup SAP tras procesar documentos."""
        informational_dialogs = {
            self.config.no_open_items_text: "no_open_items",
            self.config.batch_recorded_text: "batch_recorded",
            self.config.selected_items_text: "selected_items",
            self.config.added_items_text: "added_items",
        }
        dialogs = self.page.locator(
            "div[role='dialog'], div[role='alertdialog'], "
            "[id^='webguiPopupWindow'][id$='-contentsection']"
        )

        for _ in range(8):
            full_text = self._sap_object_error_text()
            if full_text:
                logger.error(
                    "Error SAP detectado tras procesar documentos: %s",
                    full_text,
                )
                raise SAPValidationError(full_text)

            for index in range(dialogs.count() - 1, -1, -1):
                dialog = dialogs.nth(index)
                if not dialog.is_visible():
                    continue

                dialog_text = dialog.inner_text().strip()
                for expected_text, result in informational_dialogs.items():
                    if expected_text.casefold() not in dialog_text.casefold():
                        continue

                    self._close_information_dialog(dialog)
                    logger.info(
                        "Diálogo SAP resuelto | tipo=%s | texto=%s",
                        result,
                        dialog_text,
                    )
                    return result

            self.page.wait_for_timeout(500)

        logger.info("No apareció diálogo después de procesar documentos")
        return None

    @staticmethod
    def _close_information_dialog(dialog) -> None:
        """Cierra un popup SAP informativo usando su botón OK."""
        ok_button = dialog.get_by_role("button", name="OK").first
        if not ok_button.is_visible():
            popup = dialog.locator(
                "xpath=ancestor::*[starts-with(@id, 'webguiPopupWindow')][1]"
            )
            ok_button = popup.get_by_role("button", name="OK").first
        ok_button.wait_for(state="visible", timeout=2_000)
        ok_button.click()

    def _raise_if_sap_object_error(self) -> None:
        """Lanza el texto completo cuando SAP informa un objeto inválido."""
        full_text = self._sap_object_error_text()
        if full_text:
            logger.error("Error SAP detectado: %s", full_text)
            raise SAPValidationError(full_text)

    def _detect_dialog(
            self,
            expected_text: str,
            document_created: bool = False,
        ) -> str | None:
            

            _DIALOG_SELECTOR: Final[str] = (
                "div[role='dialog'], "
                "div[role='alertdialog'], "
                "[id^='webguiPopupWindow'][id$='-contentsection']"
            )

            try:
                dialogs = self.page.locator(_DIALOG_SELECTOR)

                dialog = None
                dialog_text = ""

                # Busca desde el último elemento hacia atrás,
                # priorizando el popup más reciente que contenga
                # exactamente el texto esperado.
                for _ in range(4):
                    self._raise_if_sap_object_error()

                    for i in range(dialogs.count() - 1, -1, -1):
                        candidate = dialogs.nth(i)

                        if not candidate.is_visible():
                            continue

                        candidate_text = candidate.inner_text()

                        if expected_text in candidate_text:
                            dialog = candidate
                            dialog_text = candidate_text
                            break

                    if dialog is not None:
                        break

                    self.page.wait_for_timeout(500)

                if dialog is None:
                    logger.info(
                        "Diálogo no detectado: '%s'",
                        expected_text,
                    )
                    return None

                logger.info(
                    "Diálogo detectado: %s",
                    dialog_text,
                )

                logger.info(
                    "Diálogo esperado confirmado: '%s'",
                    expected_text,
                )

                self.page.wait_for_timeout(500)

                # ---------------------------------------------------------
                # Buscar el botón OK dentro del diálogo.
                # ---------------------------------------------------------
                ok_button = dialog.get_by_role(
                    "button",
                    name="OK",
                )

                if not ok_button.is_visible():

                    # En los popups de SAP, el contentsection puede
                    # contener el mensaje pero no el botón.
                    popup = dialog.locator(
                        "xpath=ancestor::*[starts-with(@id, 'webguiPopupWindow')][1]"
                    )

                    ok_button = popup.get_by_role(
                        "button",
                        name="OK",
                    )

                ok_button.click(timeout=2_000)

                if not document_created:
                    return dialog_text

                match = re.search(
                    r"Doc\.(\d+)",
                    dialog_text,
                )

                if match:
                    return match.group(1)

                return " "

            except Exception:
                logger.exception(
                    "Error inspeccionando diálogo modal"
                )
                raise

    # ── Wrappers semánticos sobre _detect_dialog ─────────────────────

    def _batch_recorded(self) -> bool:
        """
        Detecta el diálogo 'Se grabaron los datos. Pueden entrarse más valores.'

        Aparece cuando SAP procesa con éxito un lote de documentos y está
        listo para recibir el siguiente.

        Returns:
            True  → Diálogo detectado y cerrado.
            False → Diálogo no presente.
        """
        expected_text = self.config.batch_recorded_text
        logger.info("Verificando diálogo de confirmación de lote: '%s'", expected_text)
        return bool(self._detect_dialog(expected_text))

    def _no_open_items_found(self) -> bool:
        """
        Detecta el diálogo 'No se encontró ninguna posición de documento adecuada.'

        Indica que los documentos ingresados no tienen partidas abiertas disponibles.

        Returns:
            True  → Diálogo detectado y cerrado (no hay partidas).
            False → Diálogo no presente (hay partidas disponibles).
        """
        expected_text = self.config.no_open_items_text
        logger.info("Verificando diálogo de partidas no encontradas: '%s'", expected_text)
        return bool(self._detect_dialog(expected_text))

    def _items_selected(self) -> bool:
        """
        Detecta el diálogo informativo que contiene 'partidas seleccionadas'.

        Aparece tras seleccionar partidas para informar cuántas fueron marcadas.

        Returns:
            True  → Diálogo detectado y cerrado.
            False → Diálogo no presente.
        """
        expected_text = self.config.selected_items_text
        logger.info("Verificando diálogo de resumen de selección: '%s'", expected_text)
        return bool(self._detect_dialog(expected_text))
    
    def _items_added(self):
        """
        Detecta el diálogo informativo que contiene 'Se seleccionaron adicionalmente'.

        Aparece tras añadir todas las partidas.

        Returns:
            True  → Diálogo detectado y cerrado.
            False → Diálogo no presente.
        """
        expected_text = self.config.added_items_text
        logger.info("Verificando diálogo de resumen de selección: '%s'", expected_text)
        return bool(self._detect_dialog(expected_text))
    
    def _clearing_posted(self) -> bool:
        """
        Detecta el diálogo 'se contabilizó en sociedad'.

        Confirma que la partida de compensación fue creada exitosamente en SAP.

        Returns:
            True  → Diálogo detectado y cerrado (compensación creada).
            False → Diálogo no presente (posible error en la creación).
        """
        expected_text = self.config.clearing_posted_text
        logger.info("Verificando diálogo de confirmación de compensación: '%s'", expected_text)
        self.page.wait_for_timeout(1_500)
        self.page.wait_for_load_state("networkidle")
        return bool(self._detect_dialog(expected_text, document_created=True))