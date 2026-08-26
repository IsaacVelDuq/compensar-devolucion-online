from pathlib import Path
import logging

import pyperclip
import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError,Page
from .sap_config import FAGLL03Config
from .sap_error_recorder import ensure_error_columns, record_error

logger = logging.getLogger(__name__)


class FAGLL03ReportGenerator:
    """
    Genera reportes contables desde SAP utilizando la transacción FAGLL03.

    Reutiliza una página de navegador ya autenticada (sesión compartida).
    """

    def __init__(
        self,
        page: Page,
        company_code: str,
        accounts: str,
        date: str,
        layout: str,
        output_dir: Path,
        config: FAGLL03Config | None = None,
        df_errores: pd.DataFrame | None = None,
    ):
        self.page = page
        self.company_code = company_code
        self.accounts = accounts
        self.date = date
        self.layout = layout
        self.output_dir = output_dir
        self.config = config or FAGLL03Config(
            company_code=company_code,
            layout=layout,
        )
        self.company_code = self.config.company_code
        self.layout = self.config.layout
        self.df_errores = ensure_error_columns(df_errores)

        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        logger.info(
            "Inicializando ReportGenerator | "
            f"company_code={self.company_code} | "
            f"date={self.date} | "
            f"layout={self.layout} | "
            f"output_dir={self.output_dir}"
        )

    # ------------------------------------------------------------------
    # PUNTO DE ENTRADA
    # ------------------------------------------------------------------

    def generate(self, report_name: str) -> Path:
        """
        Ejecuta el flujo completo: navega a FAGLL03,
        ingresa los datos y descarga el archivo Excel.
        """
        logger.info(
            f"Iniciando generación de reporte | report_name={report_name}"
        )

        try:
            self._navigate_to_fagll03()
            self._fill_fields()
            self._execute()

            output_path = self._download(report_name)

            logger.info(
                f"Reporte generado correctamente | path={output_path}"
            )

            return output_path

        except PlaywrightTimeoutError as e:
            logger.exception(
                f"Timeout durante la generación del reporte: {e}"
            )
            error = RuntimeError(
                f"Timeout durante la generación del reporte: {e}"
            )
            self._record_error("generate", error)
            raise error from e

        except RuntimeError as e:
            logger.exception(
                f"RuntimeError en generación: {e}"
            )
            self._record_error("generate", e)
            raise

        except Exception as e:
            logger.exception(
                f"Error inesperado generando reporte: {e}"
            )
            error = RuntimeError(
                f"Error inesperado: {e}"
            )
            self._record_error("generate", error)
            raise error from e

    def _record_error(self, operation: str, error: BaseException) -> None:
        record_error(self.df_errores, self.__class__.__name__, operation, error)

    def get_errors(self) -> pd.DataFrame:
        """Devuelve los errores visibles registrados durante la generación."""
        return self.df_errores

    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_fagll03(self):
        """
        Ingresa a la transacción FAGLL03.
        """
        logger.info("Navegando a transacción FAGLL03")

        try:
            ok_field = self.page.locator(
                "input[id='ToolbarOkCode']"
            )

            ok_field.wait_for(
                state="visible",
                timeout=30_000,
            )

            ok_field.click()
            ok_field.fill(f"/n{self.config.transaction}")

            self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("networkidle")

            logger.info(
                "Transacción FAGLL03 cargada correctamente"
            )

        except Exception as e:
            logger.exception(
                f"Error navegando a FAGLL03: {e}"
            )
            raise

    # ------------------------------------------------------------------
    # LLENADO DE CAMPOS
    # ------------------------------------------------------------------

    def _fill_fields(self):
        """
        Llena sociedad, cuentas, fecha y layout.
        """
        logger.info("Iniciando llenado de campos")

        try:
            if not self.accounts:
                logger.error("La lista de cuentas está vacía")
                raise ValueError(
                    "La lista de cuentas está vacía."
                )

            self._fill_company_code()
            self._fill_accounts()

            # Fecha
            logger.info(
                f"Ingresando fecha: {self.date.strftime('%d.%m.%Y')}"
            )

            date_field = self.page.locator(
                "input[title='Partidas abiertas en fecha clave']"
            ).nth(0)

            date_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            date_field.fill(
                self.date.strftime("%d.%m.%Y")
            )

            # Layout
            logger.info(
                f"Ingresando layout: {self.layout}"
            )

            layout_field = self.page.locator(
                "input[title='Layout']"
            ).nth(0)

            layout_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            layout_field.fill(self.layout)

            logger.info(
                "Campos diligenciados correctamente"
            )

        except Exception as e:
            logger.exception(
                f"Error llenando campos: {e}"
            )
            raise

    def _fill_company_code(self):
        """
        Rellena el campo de sociedad.
        """
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

        except Exception as e:
            logger.exception(
                f"Error diligenciando sociedad: {e}"
            )
            raise

    def _fill_accounts(self):
        """
        Rellena el campo de cuentas.
        """
        logger.info("Diligenciando cuentas")

        try:
            account_field = self.page.locator(
                "input[title='Número de la cuenta de mayor']"
            ).nth(0)

            account_field.wait_for(
                state="visible",
                timeout=10_000,
            )

            account_field.fill(self.config.general_ledger_account)

            self.page.get_by_role(
                "button",
                name="Selección múltiple",
            ).nth(0).click()

            self._paste_into_dialog(
                self.accounts
            )

        except Exception as e:
            logger.exception(
                f"Error diligenciando cuentas: {e}"
            )
            raise

    def _paste_into_dialog(self, text: str):
        """
        Pega información en un diálogo de SAP.
        """
        logger.info(
            "Iniciando pegado en diálogo SAP"
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

        except PlaywrightTimeoutError:
            logger.exception(
                "El diálogo de selección múltiple no apareció"
            )

            raise RuntimeError(
                "El diálogo de selección múltiple no apareció a tiempo."
            )

        # Área de texto
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

        # Limpiar
        logger.info(
            "Limpiando contenido previo"
        )

        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")

        # Copiar/Pegar
        logger.info(
            "Pegando información en diálogo"
        )

        pyperclip.copy(text)

        self.page.keyboard.press("Shift+F12")
        self.page.wait_for_timeout(800)

        # Confirmar
        take_button = self._find_take_button(dialog)

        take_button.click()

        logger.info(
            "Botón Tomar presionado"
        )

        # Esperar cierre
        try:
            dialog.wait_for(
                state="hidden",
                timeout=8_000,
            )

            logger.info(
                "Diálogo cerrado correctamente"
            )

        except PlaywrightTimeoutError:
            logger.exception(
                "El diálogo no se cerró"
            )

            raise RuntimeError(
                "El diálogo de selección múltiple "
                "no se cerró tras confirmar."
            )

        self.page.locator("body").click()
        self.page.wait_for_timeout(300)

    def _find_take_button(self, dialog):
        """
        Busca el botón Tomar (F8) dentro del diálogo.
        """
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
                    f"Botón Tomar localizado | "
                    f"intento={attempt_number}"
                )

                return button

            except Exception:
                logger.warning(
                    f"Intento {attempt_number} "
                    f"falló buscando botón Tomar"
                )

        logger.error(
            "No se encontró el botón Tomar"
        )

        raise RuntimeError(
            "No se encontró el botón 'Tomar (F8)' "
            "en el diálogo de selección múltiple."
        )

    # ------------------------------------------------------------------
    # EJECUCIÓN
    # ------------------------------------------------------------------

    def _execute(self):
        """
        Ejecuta la consulta en SAP.
        """
        logger.info(
            "Ejecutando consulta SAP"
        )

        try:
            self.page.keyboard.press("F8")

            ok_button = self.page.get_by_role(
                "button",
                name="OK",
            )

            try:
                ok_button.wait_for(
                    state="visible",
                    timeout=50_000,
                )

                ok_button.click()

                logger.info(
                    "Popup OK detectado y cerrado"
                )

            except PlaywrightTimeoutError:
                logger.info(
                    "No apareció popup OK"
                )

            self.page.wait_for_load_state(
                "networkidle"
            )

            logger.info(
                "Consulta ejecutada correctamente"
            )

        except Exception as e:
            logger.exception(
                f"Error ejecutando consulta SAP: {e}"
            )
            raise

    # ------------------------------------------------------------------
    # DESCARGA
    # ------------------------------------------------------------------

    def _download(self, report_name: str) -> Path:
        """
        Descarga el reporte Excel.
        """
        logger.info(
            f"Iniciando descarga | report_name={report_name}"
        )

        output_path = (
            self.output_dir /
            f"{report_name}.xlsx"
        )

        try:
            self.page.keyboard.press("Shift+F4")

            logger.info(
                "Atajo Shift+F4 ejecutado"
            )

            file_name_field = self.page.locator(
                "[name='InputField']"
            )

            try:
                file_name_field.wait_for(
                    state="visible",
                    timeout=10_000,
                )

            except PlaywrightTimeoutError:
                logger.exception(
                    "No apareció popup de exportación"
                )

                raise RuntimeError(
                    "No apareció el campo de nombre "
                    "de fichero tras Shift+F4."
                )

            file_name_field.click(click_count=3)
            file_name_field.fill(report_name)

            logger.info(
                "Nombre del reporte diligenciado"
            )

            second_popup = False

            try:
                self.page.keyboard.press("Enter")

                self.page.wait_for_selector(
                    "text=Introducir el nombre del fichero",
                    timeout=5_000,
                )

                second_popup = True

                logger.info(
                    "Detectado flujo A de descarga"
                )

            except PlaywrightTimeoutError:
                logger.info(
                    "Detectado flujo B de descarga"
                )

            if second_popup:

                file_field = self.page.get_by_role(
                    "textbox",
                    name="Fichero",
                )

                try:
                    file_field.wait_for(
                        state="visible",
                        timeout=5_000,
                    )

                    file_field.click(click_count=3)

                    file_field.fill(
                        f"{report_name}.xlsx"
                    )

                    logger.info(
                        "Nombre de fichero diligenciado"
                    )

                except PlaywrightTimeoutError:
                    logger.exception(
                        "No se encontró campo Fichero"
                    )

                    raise RuntimeError(
                        "No se encontró el campo "
                        "'Fichero' en el popup de ruta."
                    )

                with self.page.expect_download(
                    timeout=30_000
                ) as download_info:

                    self.page.keyboard.press("Enter")

            else:

                with self.page.expect_download(
                    timeout=30_000
                ) as download_info:
                    pass

            download = download_info.value

            logger.info(
                "Descarga capturada correctamente"
            )

            download.save_as(output_path)

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

            raise RuntimeError(
                "La descarga no se completó correctamente "
                "(archivo vacío o inexistente)."
            )

        except Exception as e:
            logger.exception(
                f"Error descargando reporte: {e}"
            )
            raise