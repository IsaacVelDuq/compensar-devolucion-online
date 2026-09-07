"""
compensar_f03.py

Automatización de la transacción F-03 (Compensar partidas abiertas de
deudor) en SAP WebGUI mediante Playwright. Compensa cada partida contra
su contrapartida, ajustando diferencias de importe cuando existen.
"""

from __future__ import annotations

import datetime
import logging
import re

import pandas as pd
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .sap_config import F03Config, REPORT_COLUMNS
from .sap_exceptions import (
    SAPRPAError,
    SAPAutomationError,
    SAPNoItemsFoundError,
    SAPValidationError,
)

logger = logging.getLogger(__name__)


class F03CounterpartyClearing:
    """
    Automatiza la compensación de partidas abiertas de deudor en SAP
    mediante la transacción F-03, emparejando cada partida con su
    contrapartida.

    Si existe diferencia de importe entre ambos documentos, ajusta la
    diferencia mediante el formulario 'Eliminar diferencias' antes de
    contabilizar. La clase de documento del ajuste corresponde al
    documento con mayor importe.

    Reutiliza una página de navegador ya autenticada (sesión compartida).

    Attributes:
        page: Instancia de Playwright Page activa.
        items_for_clearing: Números de documento a compensar.
        config: Configuración de la transacción F-03.
        df_errores: Acumulador compartido de errores visibles.
    """

    def __init__(
        self,
        page: Page,
        items_for_clearing: list[str],
        config: F03Config | None = None,
    ):
        self.page = page
        self.items_for_clearing = items_for_clearing
        self.config = config or F03Config()

        logger.info(
            "Inicializando F03CounterpartyClearing | page=%s | filas_a_compensar=%d",
            self.page, len(self.items_for_clearing),
        )

    # ------------------------------------------------------------------
    # FLUJO PRINCIPAL DE COMPENSACIÓN DE CONTRAPARTIDAS
    # ------------------------------------------------------------------

    def clear(
        self,
        date: datetime.datetime,
    ) -> str:
        """
        Ejecuta el proceso completo de compensación en F-03 para
        ``self.items_for_clearing``: navega a la transacción, llena los
        campos de cabecera, ingresa los documentos a compensar, ajusta la
        diferencia de importe (si existe) y contabiliza.

        Args:
            date: Fecha de compensación a registrar.

        Returns:
            Descripción legible del resultado, incluyendo el documento creado.

        Lanza:
            SAPAutomationError: si SAP no confirma la creación de la
                compensación al finalizar el flujo.
        """
        logger.info("Iniciando proceso de compensación en SAP para contrapartidas")

        self._navigate_to_f03()
        self._fill_header_fields(
            self.config.company_code, self.config.general_ledger_account,
            self.config.currency, date,
        )
        self._fill_open_item_documents(self.items_for_clearing)
        document_class, diff = self._read_difference_fields()
        if diff > self.config.tolerance_for_cleared_items:
            logger.warning(
                "Diferencia de importe detectada: %.2f — se recomienda revisar antes de continuar",
                diff,
            )
            raise SAPAutomationError(
                f"La diferencia para ajuste de peso es muy grande para continuar \nDiferencia detectada: {diff}"
            )
        self._open_remove_differences_form()

        has_difference = diff > self.config.float_epsilon
        if has_difference:
            self._remove_differences(
                self.config.adjustment_text, document_class, self.config.adjustment_account, str(diff)
            )

        self._post()
        self._wait_for_loading()
        document = self._clearing_posted()

        if not document:
            raise SAPAutomationError(
                "No se detectó confirmación de creación de compensación"
            )

        logger.info("Compensación creada exitosamente")

        if has_difference:
            process = f"Compensación por contrapartida en {self.config.currency} y ajuste al peso por ${diff}"
        else:
            process = f"Compensación por contrapartida exacta en {self.config.currency}"

        process = process + f"\nDocumento creado: {document}"
        return process

    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_f03(self) -> None:
        """
        Navega a la transacción F-03 desde cualquier pantalla de SAP
        usando el campo ToolbarOkCode.

        Lanza:
            SAPAutomationError: si el campo de transacción no carga a tiempo
                o si ocurre cualquier otro error durante la navegación.
        """
        logger.info("Navegando a transacción F-03")

        try:
            ok_field = self.page.locator("input[id='ToolbarOkCode']")
            ok_field.wait_for(state="visible", timeout=20_000)
            ok_field.click(timeout=2_000)
            ok_field.fill(f"/n{self.config.transaction}")
            self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("networkidle")
            logger.info("Transacción F-03 cargada correctamente")

        except PlaywrightTimeoutError as e:
            logger.exception("Timeout esperando campo de transacción en F-03")
            raise SAPAutomationError(
                "Tiempo de espera agotado esperando campo de transacción en F-03"
            ) from e

        except Exception as e:
            logger.exception("Error navegando a F-03")
            raise SAPAutomationError("Error navegando a F-03") from e

    # ------------------------------------------------------------------
    # LLENADO DE CAMPOS INICIALES
    # ------------------------------------------------------------------

    def _fill_header_fields(
        self,
        company_code: str,
        account: str,
        currency: str,
        date: datetime.datetime,
    ) -> None:
        """
        Rellena los campos del formulario inicial de F-03: cuenta contable,
        sociedad, moneda y fecha de compensación. Al finalizar selecciona el
        criterio por Nº documento y hace clic en 'Tratar PAs' para cargar
        las partidas abiertas.

        Args:
            company_code: Código de sociedad SAP (p.ej. '1000').
            account: Número de cuenta contable del deudor.
            currency: Moneda del documento (p.ej. 'COP', 'USD').
            date: Fecha de compensación a registrar.

        Lanza:
            SAPValidationError: si SAP muestra un diálogo de período contable
                cerrado tras ingresar la fecha o al procesar las partidas.
            SAPAutomationError: si algún campo no carga a tiempo o si ocurre
                cualquier otro error al llenar los campos.
        """
        logger.info(
            "Llenando campos F-03 | sociedad=%s | cuenta=%s | fecha=%s",
            company_code, account, date.strftime("%d.%m.%Y"),
        )

        try:
            # ── Cuenta contable ──────────────────────────────────────────
            account_field = self.page.get_by_role("textbox", name="Cuenta Necesarios")
            account_field.wait_for(state="visible", timeout=1_500)
            account_field.fill(str(account))
            logger.info("Cuenta ingresada: %s", account)

            # ── Sociedad ─────────────────────────────────────────────────
            company_code_field = self.page.get_by_role("textbox", name="Sociedad Necesarios")
            company_code_field.wait_for(state="visible", timeout=1_500)
            company_code_field.fill(str(company_code))
            logger.info("Sociedad ingresada: %s", company_code)

            # ── Moneda ───────────────────────────────────────────────────
            currency_field = self.page.get_by_role("textbox", name="Moneda")
            currency_field.wait_for(state="visible", timeout=1_500)
            currency_field.fill(str(currency))
            logger.info("Moneda ingresada: %s", currency)

            # ── Fecha de compensación ────────────────────────────────────
            date_field = self.page.get_by_role("textbox", name="Fe.compensación Necesarios")
            date_field.wait_for(state="visible", timeout=1_500)
            date_field.fill(date.strftime("%d.%m.%Y"))
            logger.info("Fecha ingresada: %s", date.strftime("%d.%m.%Y"))

            dialog_text = self._detect_dialog(self.config.accounting_period_dialog_text)
            if dialog_text:
                error = SAPValidationError(dialog_text)
                raise error

            # ── Criterio de búsqueda: Nº documento ───────────────────────
            self.page.get_by_role("radio", name=self.config.document_number_label).check()
            logger.info("Radio '%s' seleccionado", self.config.document_number_label)

            # ── Lanzar búsqueda de partidas abiertas ─────────────────────
            self.page.get_by_role("button", name=self.config.process_items_label).click(timeout=2_000)
            logger.info(
                "Botón '%s' presionado — esperando carga de partidas",
                self.config.process_items_label,
            )

            dialog_text = self._detect_dialog(self.config.accounting_period_dialog_text)
            if dialog_text:
                raise SAPValidationError(dialog_text)

        except PlaywrightTimeoutError as e:
            logger.exception("Timeout llenando campos de F-03")
            raise SAPAutomationError(f"Tiempo de espera agotado llenando campos de F-03: {e}") from e

        except SAPRPAError:
            raise

        except Exception as e:
            logger.exception("Error llenando campos de F-03")
            raise SAPAutomationError(f"Error llenando campos de F-03: {e}") from e

    def _wait_for_loading(self) -> None:
        """Espera a que el indicador de carga de SAP desaparezca."""
        self.page.wait_for_timeout(1_500)
        self.page.wait_for_load_state("networkidle")
        try:
            self.page.locator("#ur-loading-footer").wait_for(state="visible", timeout=5_000)
            self.page.locator("#ur-loading-footer").wait_for(state="hidden", timeout=300_000)
        except Exception:
            logger.debug("No se detectó indicador de carga, continuando...")

    # ------------------------------------------------------------------
    # DETECCIÓN DE DIÁLOGOS SAP
    # ------------------------------------------------------------------

    def _detect_dialog(self, expected_text: str, document_created: bool = False) -> str | None:
        """
        Espera, valida y cierra un diálogo modal de SAP.

        Intenta hasta 4 veces (cada 500 ms) antes de determinar que el diálogo
        no está presente, compensando los tiempos de renderizado de SAP Web GUI.

        Args:
            expected_text: Fragmento que debe aparecer en el diálogo para
                considerarlo válido y cerrarlo.
            document_created: Si True, extrae el número de documento SAP del
                patrón "Doc.<número>" antes de retornar.

        Returns:
            - Si ``document_created=True``:
                - El número de documento como str (ej. ``"3803645132"``), o
                - ``" "`` (espacio) si el diálogo fue válido pero no contenía
                  número — preserva el valor truthy del retorno para el caller.
            - Si ``document_created=False``:
                - El texto completo del diálogo si ``expected_text`` estaba
                  presente.
            - ``None`` si el diálogo no era visible o no contenía ``expected_text``.

        Note:
            El caller puede distinguir "diálogo detectado sin número" de "no
            detectado" con: ``if resultado and resultado.strip()``.

        Lanza:
            Exception: si ocurre un error inesperado al inspeccionar el DOM.
        """
        try:
            dialog = self.page.locator("div[role='dialog']")

            # SAP Web GUI puede tardar varios ciclos en renderizar el diálogo modal;
            # se espera hasta 2 s en total antes de desistir.
            for _ in range(4):
                if dialog.is_visible():
                    break
                self.page.wait_for_timeout(500)

            if not dialog.is_visible():
                return None  # El diálogo nunca apareció

            dialog_text = dialog.inner_text()
            logger.info("Diálogo detectado: %s", dialog_text)

            if expected_text not in dialog_text:
                # El diálogo existe pero no es el que esperábamos (ej. otro error de SAP)
                return None

            logger.info("Diálogo esperado confirmado: '%s'", expected_text)

            # Dar un breve margen antes de cerrar para que SAP termine de renderizar
            self.page.wait_for_timeout(500)
            self.page.get_by_role("button", name="OK").click(timeout=2_000)
            self.page.wait_for_load_state("networkidle")

            if not document_created:
                return dialog_text

            # Extraer número de documento del patrón "Doc.XXXXXXXXXX"
            match = re.search(r"Doc\.(\d+)", dialog_text)
            if match:
                return match.group(1)

            # Diálogo válido pero sin número de documento (ej. compensación sin
            # contabilización); retornamos " " en lugar de None para mantener
            # el valor truthy en el caller.
            return " "

        except Exception:
            logger.exception("Error inspeccionando diálogo modal")
            raise

    # ── Wrappers semánticos sobre _detect_dialog ──────────────────────────

    def _batch_recorded(self) -> bool:
        """
        Detecta el diálogo 'Se grabaron los datos. Pueden entrarse más valores.'

        Aparece cuando SAP procesa con éxito un lote de documentos y está listo
        para recibir el siguiente.

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

    def _clearing_posted(self) -> str | None:
        """
        Detecta el diálogo 'se contabilizó en sociedad'.

        Confirma que la partida de compensación fue creada exitosamente en SAP.

        Returns:
            El número de documento creado si SAP confirmó la compensación,
            o None si no se detectó confirmación.
        """
        expected_text = self.config.clearing_posted_text
        logger.info("Verificando diálogo de confirmación de compensación: '%s'", expected_text)
        self.page.wait_for_timeout(1_500)
        self.page.wait_for_load_state("networkidle")
        return self._detect_dialog(expected_text, document_created=True)

    # ------------------------------------------------------------------
    # LLENADO DE DOCUMENTOS
    # ------------------------------------------------------------------

    def _fill_open_item_documents(self, documents: list[str]) -> bool:
        """
        Ingresa números de documento en los campos 'De' de la tabla de
        partidas abiertas en F-03.

        SAP renderiza un número limitado de filas según el zoom del navegador.
        Cuando se llenan todos los campos visibles se presiona Enter para que
        SAP procese el lote y habilite más campos. El ciclo se repite hasta
        agotar la lista completa de documentos.

        Nota sobre índices: SAP incluye un campo de cabecera 'De' que ocupa
        nth(0), por lo que los campos editables comienzan en nth(1). La
        variable `field_index` representa siempre el índice del campo
        editable actual.

        Args:
            documents: Lista de strings con los números de documento a ingresar.

        Returns:
            True → Todos los documentos fueron ingresados correctamente.

        Lanza:
            SAPNoItemsFoundError: si SAP reporta que no hay partidas abiertas
                disponibles para compensar.
            SAPAutomationError: si no se encuentran campos 'De' disponibles,
                si un campo no responde en el tiempo esperado, o para
                cualquier otro error inesperado.
        """
        logger.info("Iniciando llenado de documentos | total_documentos=%d", len(documents))

        try:
            # Esperar a que SAP finalice la carga de la pantalla de partidas
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(1_500)

            # Contar campos editables disponibles (se resta 1 por la cabecera)
            # Hasta 4 reintentos de 500 ms si SAP aún no los ha renderizado
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
                    "números de documento en F-03."
                )

            field_index = 1        # nth(1) = primer campo editable (nth(0) es la cabecera)
            total_documents = len(documents)

            for i in range(total_documents):
                self.page.wait_for_timeout(300)

                field = self.page.get_by_role("textbox", name="De").nth(field_index)

                try:
                    # Hasta 4 reintentos de 500 ms si el campo aún no es visible
                    field.wait_for(state="visible", timeout=500)
                    for _ in range(4):
                        if not field.is_visible():
                            self.page.wait_for_timeout(500)
                        else:
                            break

                    field.fill(documents[i])
                    logger.info(
                        "Documento ingresado | pos=%d | doc=%s | progreso=%d/%d",
                        field_index, documents[i], i + 1, total_documents,
                    )

                except PlaywrightTimeoutError as e:
                    logger.exception(
                        "Timeout esperando campo 'De' en posición %d para documento %s",
                        field_index, documents[i],
                    )
                    raise SAPAutomationError(
                        f"Tiempo de espera agotado esperando el campo para "
                        f"ingresar el documento '{documents[i]}'."
                    ) from e

                # ── Último documento: cerrar el formulario ───────────────
                if i == total_documents - 1:
                    logger.info(
                        "Último documento ingresado — presionando '%s' para finalizar",
                        self.config.process_items_label,
                    )
                    self.page.get_by_role(
                        "button", name=self.config.process_items_label
                    ).click(timeout=2_000)
                    self.page.wait_for_load_state("networkidle")

                    if self._no_open_items_found():
                        raise SAPNoItemsFoundError(
                            "SAP reportó que no hay partidas abiertas para compensar."
                        )

                # ── Lote completo: enviar a SAP y preparar siguiente lote ─
                elif field_index == total_fields:
                    logger.info("Lote completo (%d campos) — enviando a SAP con Enter", total_fields)
                    self.page.keyboard.press("Enter")
                    self.page.wait_for_load_state("networkidle")
                    self.page.wait_for_timeout(500)

                    if self._no_open_items_found():
                        raise SAPNoItemsFoundError(
                            "SAP reportó que no hay partidas abiertas para compensar."
                        )

                    if self._batch_recorded():
                        # SAP limpió los campos; reiniciar índice para el siguiente lote
                        logger.info("Lote grabado — reiniciando índice de campos para el siguiente lote")
                        field_index = 1
                        continue

                # ── Avanzar al siguiente campo dentro del lote actual ────
                else:
                    field_index += 1

            logger.info(
                "Llenado de documentos completado exitosamente | total_ingresados=%d",
                total_documents,
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
    # APERTURA DE FORMULARIO 'ELIMINAR DIFERENCIAS'
    # ------------------------------------------------------------------

    def _open_remove_differences_form(self) -> None:
        """
        hace clic en el botón 'Eliminar diferencias' para abrir su formulario.
        """
        logger.info("Abriendo formulario 'Eliminar diferencias' tras selección de partidas")

        self.page.wait_for_load_state("networkidle")

        remove_differences_button = self.page.get_by_role(
            "button", name=self.config.remove_differences_label
        )
        remove_differences_button.wait_for(state="visible", timeout=1_500)
        remove_differences_button.click(timeout=2_000)
        logger.info("Formulario 'Eliminar diferencias' abierto")
        self.page.wait_for_load_state("networkidle")

    # ------------------------------------------------------------------
    # ELIMINACIÓN DE DIFERENCIAS
    # ------------------------------------------------------------------

    def _remove_differences(
        self,
        text: str,
        document_class: str,
        adjustment_account: str,
        difference_value: str,
        tax_indicator: str | None = None,
    ) -> None:
        """
        Rellena el formulario 'Eliminar diferencias' para el ajuste de la
        diferencia de importe entre la partida y su contrapartida.

        El flujo presiona Enter tras ingresar la cuenta de ajuste para que SAP
        exponga los campos de importe e indicador de impuestos.

        Args:
            text: Texto de referencia y cabecera del documento de ajuste.
            document_class: Clave de clase de documento SAP (ClvCT).
            adjustment_account: Cuenta contable de ajuste (p.ej. '5395950001').
            difference_value: Importe de la diferencia a registrar (como string).
            tax_indicator: Indicador de impuestos SAP (por defecto el
                configurado en ``self.config.default_tax_indicator``).
        """
        tax_indicator = tax_indicator or self.config.default_tax_indicator
        self.page.wait_for_load_state("networkidle")

        # ── Referencia ───────────────────────────────────────────────────
        reference_field = self.page.get_by_role("textbox", name="Referencia")
        reference_field.wait_for(state="visible", timeout=1_500)
        reference_field.fill(text)
        logger.info("Referencia ingresada: %s", text)

        # ── Texto de cabecera ────────────────────────────────────────────
        header_text_field = self.page.get_by_role("textbox", name="Txt.cab.doc.")
        header_text_field.wait_for(state="visible", timeout=1_500)
        header_text_field.fill(text)
        logger.info("Texto de cabecera ingresado: %s", text)

        # ── Clase de documento (ClvCT) ───────────────────────────────────
        document_class_field = self.page.get_by_role("textbox", name="ClvCT")
        document_class_field.wait_for(state="visible", timeout=1_500)
        document_class_field.fill(document_class)
        logger.info("Clase de documento ingresada: %s", document_class)

        # ── Cuenta de ajuste ─────────────────────────────────────────────
        account_field = self.page.get_by_role("textbox", name="Cuenta")
        account_field.wait_for(state="visible", timeout=1_500)
        account_field.fill(adjustment_account)
        logger.info("Cuenta de ajuste ingresada: %s", adjustment_account)

        # ── Enter para que SAP exponga campos de importe e impuestos ─────
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(500)
        self.page.keyboard.press("Enter")
        self.page.wait_for_load_state("networkidle")

        # ── Importe de la diferencia ─────────────────────────────────────
        amount_field = self.page.get_by_role("textbox", name="Importe")
        amount_field.wait_for(state="visible", timeout=1_500)
        amount_field.fill(difference_value)
        logger.info("Importe de diferencia ingresado: %s", difference_value)

        # ── Indicador de impuestos ───────────────────────────────────────
        tax_indicator_field = self.page.get_by_role("textbox", name="Ind.impuestos")
        tax_indicator_field.wait_for(state="visible", timeout=1_500)
        tax_indicator_field.fill(tax_indicator)
        logger.info("Indicador de impuestos ingresado: %s", tax_indicator)

    # ------------------------------------------------------------------
    # CONTABILIZACIÓN
    # ------------------------------------------------------------------

    def _post(self) -> None:
        """Hace clic en 'Contabilizar Resaltado' para registrar la compensación."""
        logger.info("Ejecutando compensación")
        self.page.wait_for_load_state("networkidle")
        post_button = self.page.get_by_role("button", name=self.config.post_button_label)
        self.page.wait_for_timeout(500)
        post_button.click(timeout=10_000)
        self.page.wait_for_load_state("networkidle")

    # ------------------------------------------------------------------
    # UTILIDADES
    # ------------------------------------------------------------------

    def _update_open_items(self, documents: list[str]) -> pd.DataFrame:
        """Elimina del DataFrame de partidas los documentos ya compensados."""
        for document in documents:
            self.df_open_items = self.df_open_items[
                self.df_open_items[REPORT_COLUMNS.document_number] != document
            ]
        return self.df_open_items

        # ------------------------------------------------------------------
    # LECTURA DE IMPORTES SAP
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sap_amount(value: str) -> float:
        """
        Convierte un importe en formato SAP (coma decimal, punto de miles,
        signo negativo al final) a float.

        Ejemplos:
            "49-"       -> -49.0
            "1.234,56"  -> 1234.56
            "1.234,56-" -> -1234.56
        """
        value = value.strip()
        if not value:
            return 0.0

        negative = value.endswith("-")
        if negative:
            value = value[:-1].strip()

        value = value.replace(".", "").replace(",", ".")

        try:
            amount = float(value)
        except ValueError:
            logger.warning("No se pudo convertir importe SAP a float: '%s'", value)
            return 0.0

        return -amount if negative else amount

    def _read_difference_fields(self) -> tuple[str | None, float]:
        """
        Lee 'Importe entrado' y 'Asignados' en la pantalla de partidas
        abiertas de F-03 para determinar la clase de documento del ajuste
        y el valor absoluto de la diferencia a compensar.

        Si 'Importe entrado' es distinto de cero se usa clase "50"; si es
        cero pero 'Asignados' es distinto de cero, se usa "40". El valor
        de `diff` siempre corresponde al valor absoluto de 'Asignados'.

        Returns:
            Tupla (document_class, diff).

        Lanza:
            SAPAutomationError: si los campos no cargan a tiempo, o si
                ninguno de los dos presenta una diferencia distinta de cero.
        """
        try:
            self.page.wait_for_load_state("networkidle")
            self._items_selected()
            self.page.wait_for_load_state("networkidle")
            importe_entrado_field = self.page.get_by_role("textbox", name="Importe entrado")
            importe_entrado_field.wait_for(state="visible", timeout=1_500)
            importe_entrado_raw = importe_entrado_field.input_value()

            asignados_field = self.page.get_by_role("textbox", name="Asignados")
            asignados_field.wait_for(state="visible", timeout=1_500)
            asignados_raw = asignados_field.input_value()

        except PlaywrightTimeoutError as e:
            logger.exception("Timeout leyendo campos 'Importe entrado'/'Asignados'")
            raise SAPAutomationError(
                "Tiempo de espera agotado leyendo 'Importe entrado'/'Asignados'"
            ) from e

        importe_entrado = self._parse_sap_amount(importe_entrado_raw)
        asignados = self._parse_sap_amount(asignados_raw)

        logger.info(
            "Importe entrado=%s (%.2f) | Asignados=%s (%.2f)",
            importe_entrado_raw, importe_entrado, asignados_raw, asignados,
        )

        if abs(importe_entrado) > self.config.float_epsilon:
            document_class = self.config.adjustment_document_class_debit
        elif abs(asignados) > self.config.float_epsilon:
            document_class = self.config.adjustment_document_class_credit
        else:
            document_class = None

        diff = int(abs(asignados))
        return document_class, diff
