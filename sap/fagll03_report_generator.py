from __future__ import annotations

import logging
from pathlib import Path

import psutil
import pyperclip
import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Page
from .sap_config import FAGLL03Config
from .sap_error_recorder import ensure_error_columns, record_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BASE COMPARTIDA
# ---------------------------------------------------------------------------

class _FAGLL03Base:
    """
    Lógica común a cualquier automatización sobre FAGLL03: navegación a
    la transacción, diligenciamiento de sociedad/cuentas, manejo de los
    diálogos de "Selección múltiple", ejecución de la consulta (F8) y
    descarga del resultado a Excel.

    No se instancia directamente: `FAGLL03ReportGenerator` y
    `FAGLL03ClearedItemsReportGenerator` heredan de esta clase. Ambas
    subclases deben definir `self.output_dir` en su `__init__` antes de
    llamar a `_download`.
    """

    def __init__(
        self,
        page: Page,
        company_code: str,
        accounts: str,
        config: FAGLL03Config | None = None,
        df_errores: pd.DataFrame | None = None,
    ):
        self.page = page
        self.company_code = company_code
        self.accounts = accounts
        self.config = config or FAGLL03Config(company_code=company_code)
        self.company_code = self.config.company_code
        self.df_errores = ensure_error_columns(df_errores)

    def _record_error(self, operation: str, error: BaseException) -> None:
        record_error(self.df_errores, self.__class__.__name__, operation, error)

    def get_errors(self) -> pd.DataFrame:
        """Devuelve los errores visibles registrados durante la ejecución."""
        return self.df_errores

    # ------------------------------------------------------------------
    # DIAGNÓSTICO DE MEMORIA
    # ------------------------------------------------------------------

    def _log_diag(self, label: str):
        """
        Registra memoria del sistema y del proceso del navegador en el
        momento exacto en que se llama. Se usa para diagnosticar si el
        crash del navegador durante la descarga está relacionado con
        presión de memoria (confirmado en dumps: excepción 0xE0000008,
        el código que Chromium usa deliberadamente cuando detecta OOM).
        """
        try:
            vm = psutil.virtual_memory()
            browser_mb = sum(
                p.info["memory_info"].rss / (1024**2)
                for p in psutil.process_iter(["name", "memory_info"])
                if p.info["name"] in ("chrome.exe", "msedge.exe")
            )
            logger.warning(
                f"[DIAG {label}] "
                f"RAM sistema: {vm.percent}% en uso | "
                f"disponible={vm.available / (1024**2):.0f} MB de {vm.total / (1024**2):.0f} MB | "
                f"RAM navegador (todos los procesos): {browser_mb:.0f} MB"
            )
        except Exception as e:
            logger.warning(f"[DIAG {label}] No se pudo capturar snapshot: {e}")

    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_fagll03(self):
        """
        Ingresa a la transacción FAGLL03.
        """
        logger.info("Navegando a transacción FAGLL03")

        try:
            ok_field = self.page.locator("input[id='ToolbarOkCode']")
            ok_field.wait_for(state="visible", timeout=30_000)

            ok_field.click()
            ok_field.fill(f"/n{self.config.transaction}")

            self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("networkidle")

            logger.info("Transacción FAGLL03 cargada correctamente")

        except Exception as e:
            logger.exception(f"Error navegando a FAGLL03: {e}")
            raise

    # ------------------------------------------------------------------
    # SOCIEDAD / CUENTAS
    # ------------------------------------------------------------------

    def _fill_company_code(self):
        """
        Rellena el campo de sociedad.
        """
        logger.info("Diligenciando sociedad")

        try:
            company_code_field = self.page.locator(
                "input[title='Sociedad']"
            ).nth(0)

            company_code_field.wait_for(state="visible", timeout=10_000)
            company_code_field.fill(self.config.company_code)

            self.page.get_by_role(
                "button", name="Selección múltiple"
            ).nth(1).click()

            self._paste_into_dialog(self.company_code)

        except Exception as e:
            logger.exception(f"Error diligenciando sociedad: {e}")
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

            account_field.wait_for(state="visible", timeout=10_000)
            account_field.fill(self.config.general_ledger_account)

            self.page.get_by_role(
                "button", name="Selección múltiple"
            ).nth(0).click()

            self._paste_into_dialog(self.accounts)

        except Exception as e:
            logger.exception(f"Error diligenciando cuentas: {e}")
            raise

    # ------------------------------------------------------------------
    # DIÁLOGO DE SELECCIÓN MÚLTIPLE
    # ------------------------------------------------------------------

    def _paste_into_dialog(self, text: str, dialog=None):
        """
        Pega información en un diálogo de "Selección múltiple" de SAP ya
        visible y confirma con el botón "Tomar (F8)".

        Args:
            text: contenido a pegar (uno o varios valores separados por
                salto de línea).
            dialog: locator del diálogo a usar. Si no se indica, se
                localiza el primer `div[role='dialog']` visible en
                pantalla (comportamiento por defecto, usado para
                sociedad/cuentas).
        """
        logger.info("Iniciando pegado en diálogo SAP")

        if dialog is None:
            dialog = self.page.locator("div[role='dialog']")

        try:
            dialog.wait_for(state="visible", timeout=10_000)
            logger.info("Diálogo SAP detectado")

        except PlaywrightTimeoutError:
            logger.exception("El diálogo de selección múltiple no apareció")
            raise RuntimeError(
                "El diálogo de selección múltiple no apareció a tiempo."
            )

        text_area = dialog.locator("textarea, input[type='text']").first

        try:
            text_area.wait_for(state="visible", timeout=5_000)
            text_area.click()
            logger.info("Área de texto enfocada")

        except PlaywrightTimeoutError:
            logger.warning("No se encontró área de texto; usando fallback")
            dialog.click()

        logger.info("Limpiando contenido previo")
        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")

        logger.info("Pegando información en diálogo")
        pyperclip.copy(text)
        self.page.keyboard.press("Shift+F12")
        self.page.wait_for_timeout(800)

        take_button = self._find_take_button(dialog)
        take_button.click()
        logger.info("Botón Tomar presionado")

        try:
            dialog.wait_for(state="hidden", timeout=8_000)
            logger.info("Diálogo cerrado correctamente")

        except PlaywrightTimeoutError:
            logger.warning(
                "El diálogo sigue visible después de 'Tomar'; "
                "se confirma con Enter como respaldo"
            )
            dialog.press("Enter")
            try:
                dialog.wait_for(state="hidden", timeout=5_000)
                logger.info("Diálogo cerrado con Enter")
            except PlaywrightTimeoutError as error:
                logger.exception("El diálogo no se cerró después de Tomar y Enter")
                raise RuntimeError(
                    "El diálogo de selección múltiple no se cerró tras confirmar."
                ) from error

        self.page.locator("body").click()
        self.page.wait_for_timeout(300)

    def _find_take_button(self, dialog):
        """
        Busca el botón Tomar (F8) en el contenido o en el popup SAP.

        En SAP WebGUI el contenido suele estar en un ``*-contentsection``
        y los botones en el footer hermano del mismo popup.
        """
        logger.info("Buscando botón 'Tomar (F8)'")

        popup = dialog.locator(
            "xpath=ancestor-or-self::*[starts-with(@id, 'webguiPopupWindow')][1]"
        )
        scopes = (dialog, popup, self.page)
        attempts = []
        for scope in scopes:
            attempts.extend(
                [
                    lambda scope=scope: scope.get_by_role(
                        "button", name="Tomar (F8)"
                    ).first,
                    lambda scope=scope: scope.get_by_role(
                        "button", name="Tomar"
                    ).first,
                    lambda scope=scope: scope.locator(
                        "[role='button'][title='Tomar (F8)']"
                    ).first,
                    lambda scope=scope: scope.locator(
                        "[role='button'][title='Tomar']"
                    ).first,
                    lambda scope=scope: scope.locator(
                        "[title*='Tomar']"
                    ).first,
                ]
            )

        for attempt_number, attempt in enumerate(attempts, start=1):
            try:
                button = attempt()
                button.wait_for(state="visible", timeout=3_000)
                logger.info(f"Botón Tomar localizado | intento={attempt_number}")
                return button

            except Exception:
                logger.warning(f"Intento {attempt_number} falló buscando botón Tomar")

        logger.error("No se encontró el botón Tomar")
        raise RuntimeError(
            "No se encontró el botón 'Tomar (F8)' en el diálogo de "
            "selección múltiple."
        )

    # ------------------------------------------------------------------
    # EJECUCIÓN
    # ------------------------------------------------------------------

    def _execute(self):
        """
        Ejecuta la consulta en SAP (F8) y cierra el popup OK si aparece.
        """
        logger.info("Ejecutando consulta SAP")

        try:
            self.page.keyboard.press("F8")

            ok_button = self.page.get_by_role("button", name="OK")

            try:
                ok_button.wait_for(state="visible", timeout=50_000)
                ok_button.click()
                logger.info("Popup OK detectado y cerrado")

            except PlaywrightTimeoutError:
                logger.info("No apareció popup OK")

            self._wait_for_execution_result()
            logger.info("Consulta ejecutada correctamente")

        except Exception as e:
            logger.exception(f"Error ejecutando consulta SAP: {e}")
            raise

    def _wait_for_execution_result(self):
        """
        Espera, con polling, a que SAP termine de procesar la consulta
        tras F8.

        FAGLL03 puede tardar varios minutos en devolver el resultado
        (igual que FBL5N), así que en vez de un único
        `wait_for_load_state("networkidle")` con timeout fijo, se
        reintenta hasta `self.config.max_result_attempts` veces,
        esperando `self.config.result_poll_interval_ms` entre intentos.
        """
        max_attempts = self.config.max_result_attempts
        interval_ms = self.config.result_poll_interval_ms

        logger.info(
            f"Iniciando espera de resultado tras F8 | "
            f"max_attempts={max_attempts} | interval_ms={interval_ms}"
        )

        for attempt in range(1, max_attempts + 1):
            try:
                self.page.wait_for_load_state("networkidle", timeout=interval_ms)
                logger.info(f"Resultado disponible tras F8 | intento={attempt}")
                return

            except PlaywrightTimeoutError:
                continue

        total_seconds = max_attempts * interval_ms / 1000
        logger.warning(
            f"No se detectó 'networkidle' tras {max_attempts} intentos "
            f"(~{total_seconds:.0f}s); se continúa de todas formas"
        )

    # ------------------------------------------------------------------
    # DESCARGA A EXCEL
    # ------------------------------------------------------------------

    def _download(self, report_name: str) -> Path:

        logger.info(f"Iniciando descarga | report_name={report_name}")

        output_path = self.output_dir / f"{report_name}.xlsx"

        self._log_diag("inicio de _download")

        try:
            self.page.keyboard.press("Shift+F4")
            logger.info("Atajo Shift+F4 ejecutado")

            file_name_field = self.page.locator("[name='InputField']")

            try:
                file_name_field.wait_for(state="visible", timeout=10_000)

            except PlaywrightTimeoutError:
                logger.exception("No apareció popup de exportación")
                raise RuntimeError(
                    "No apareció el campo de nombre de fichero tras Shift+F4."
                )

            file_name_field.click(click_count=3)
            file_name_field.fill(report_name)
            logger.info("Nombre del reporte diligenciado")

            second_popup = False

            # Listener de red: registra el peso real de las respuestas no
            # triviales durante la descarga, para comparar si SAP está
            # mandando un archivo distinto/más pesado en la descarga
            # que falla.
            def _on_response(response):
                try:
                    cl = response.headers.get("content-length", "?")
                    ct = response.headers.get("content-type", "?")
                    if cl != "?" and int(cl) > 10_000:
                        logger.info(
                            f"[DIAG-RED] status={response.status} size={cl} bytes "
                            f"type={ct} url={response.url[:100]}"
                        )
                except Exception:
                    pass

            self.page.on("response", _on_response)

            try:
                self.page.keyboard.press("Enter")

                self.page.wait_for_selector(
                    "text=Introducir el nombre del fichero",
                    timeout=5_000,
                )
                second_popup = True
                logger.info("Detectado flujo A de descarga")

            except PlaywrightTimeoutError:
                try:
                    logger.info("DIntentando tomar botón ok nuevamente")
                    ok_button = self.page.get_by_role("button", name="OK")
                    ok_button.click(timeout=5_000)
                except:
                    logger.info("Detectado flujo B de descarga")

            self._log_diag("tras detectar flujo A/B")

            if second_popup:
                file_field = self.page.get_by_role("textbox", name="Fichero")

                try:
                    file_field.wait_for(state="visible", timeout=5_000)
                    file_field.click(click_count=3)
                    file_field.fill(f"{report_name}.xlsx")
                    logger.info("Nombre de fichero diligenciado")

                except PlaywrightTimeoutError:
                    logger.exception("No se encontró campo Fichero")
                    raise RuntimeError(
                        "No se encontró el campo 'Fichero' en el popup de ruta."
                    )

                self._log_diag("justo antes de expect_download (flujo A)")
                with self.page.expect_download(timeout=30_000) as download_info:
                    self.page.keyboard.press("Enter")

            else:
                self._log_diag("justo antes de expect_download (flujo B)")
                with self.page.expect_download(timeout=30_000) as download_info:
                    export_ok_button = self.page.get_by_role("button", name="OK")

                    try:
                        export_ok_button.wait_for(state="visible", timeout=5_000)
                        export_ok_button.click()
                        logger.info("Popup OK de exportación detectado y cerrado")

                    except PlaywrightTimeoutError:
                        logger.info(
                            "No apareció popup OK de exportación; "
                            "se asume descarga automática tras Enter"
                        )

            download = download_info.value
            logger.info("Descarga capturada correctamente")

            try:
                suggested = download.suggested_filename
                logger.warning(f"[DIAG] Descarga capturada | nombre_sugerido={suggested}")
            except Exception as e:
                logger.warning(f"[DIAG] No se pudo leer metadata de la descarga: {e}")

            self._log_diag("justo antes de save_as")

            download.save_as(output_path)
            logger.info(f"Archivo guardado | path={output_path}")

            self._log_diag("después de save_as (llegó sin fallar)")

            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"Reporte descargado correctamente | path={output_path}")
                return output_path

            logger.error("Archivo descargado vacío o inexistente")
            raise RuntimeError(
                "La descarga no se completó correctamente "
                "(archivo vacío o inexistente)."
            )

        except Exception as e:
            self._log_diag("EN EL MOMENTO DE LA EXCEPCIÓN")
            logger.exception(f"Error descargando reporte: {e}")
            raise


# ---------------------------------------------------------------------------
# PARTIDAS ABIERTAS -> DESCARGA A EXCEL (flujo original)
# ---------------------------------------------------------------------------

class FAGLL03ReportGenerator(_FAGLL03Base):
    """
    Genera reportes contables desde SAP utilizando la transacción FAGLL03
    en modo "partidas abiertas", descargando el resultado como Excel.

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
        super().__init__(
            page=page,
            company_code=company_code,
            accounts=accounts,
            config=config or FAGLL03Config(company_code=company_code, layout=layout),
            df_errores=df_errores,
        )

        self.date = date
        self.layout = self.config.layout
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Inicializando FAGLL03ReportGenerator | "
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
        Ejecuta el flujo completo: navega a FAGLL03, ingresa los datos y
        descarga el archivo Excel.
        """
        logger.info(f"Iniciando generación de reporte | report_name={report_name}")

        try:
            self._navigate_to_fagll03()
            self._fill_fields()
            self._execute()

            output_path = self._download(report_name)

            logger.info(f"Reporte generado correctamente | path={output_path}")
            return output_path

        except PlaywrightTimeoutError as e:
            logger.exception(f"Timeout durante la generación del reporte: {e}")
            error = RuntimeError(f"Timeout durante la generación del reporte: {e}")
            self._record_error("generate", error)
            raise error from e

        except RuntimeError as e:
            logger.exception(f"RuntimeError en generación: {e}")
            self._record_error("generate", e)
            raise

        except Exception as e:
            logger.exception(f"Error inesperado generando reporte: {e}")
            error = RuntimeError(f"Error inesperado: {e}")
            self._record_error("generate", error)
            raise error from e

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
                raise ValueError("La lista de cuentas está vacía.")

            self._fill_company_code()
            self._fill_accounts()

            logger.info(f"Ingresando fecha: {self.date.strftime('%d.%m.%Y')}")

            date_field = self.page.locator(
                "input[title='Partidas abiertas en fecha clave']"
            ).nth(0)

            date_field.wait_for(state="visible", timeout=10_000)
            date_field.fill(self.date.strftime("%d.%m.%Y"))

            logger.info(f"Ingresando layout: {self.layout}")

            layout_field = self.page.locator("input[title='Layout']").nth(0)
            layout_field.wait_for(state="visible", timeout=10_000)
            layout_field.fill(self.layout)

            logger.info("Campos diligenciados correctamente")

        except Exception as e:
            logger.exception(f"Error llenando campos: {e}")
            raise


# ---------------------------------------------------------------------------
# PARTIDAS COMPENSADAS -> DESCARGA A EXCEL (nuevo flujo)
# ---------------------------------------------------------------------------

class FAGLL03ClearedItemsReportGenerator(_FAGLL03Base):
    """
    Genera el reporte de "partidas compensadas" desde FAGLL03,
    descargándolo directamente a Excel.

    A diferencia de la versión anterior (`FAGLL03ClearedItemsMatcher`),
    esta clase NO filtra por importe dentro de SAP ni lee la tabla
    renderizada del DOM: descarga el reporte completo, igual que
    `FAGLL03ReportGenerator`. El filtrado, la normalización y el cruce
    contra `pending` quedan a cargo de `Fagll03ClearedItemsLoader` y
    `Fagll03ClearedItemsMatcher` (en pandas, fuera de SAP).

    Reutiliza una página de navegador ya autenticada (sesión compartida).
    """

    # TODO: confirmar contra SAP real el texto exacto de este mensaje
    # (o los mensajes equivalentes) cuando no hay partidas compensadas
    # para los criterios de selección dados.
    NO_DATA_MESSAGE = "No existen partidas que cumplan con los criterios de selección"

    def __init__(
        self,
        page: Page,
        company_code: str,
        accounts: str,
        date: pd.Timestamp,
        output_dir: Path,
        config: FAGLL03Config | None = None,
        df_errores: pd.DataFrame | None = None,
    ):
        super().__init__(
            page=page,
            company_code=company_code,
            accounts=accounts,
            config=config,
            df_errores=df_errores,
        )

        self.date = pd.Timestamp(date)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Inicializando FAGLL03ClearedItemsReportGenerator | "
            f"company_code={self.company_code} | "
            f"date={self.date} | "
            f"output_dir={self.output_dir}"
        )

    # ------------------------------------------------------------------
    # PUNTO DE ENTRADA
    # ------------------------------------------------------------------

    def generate(self, report_name: str) -> Path | None:
        """
        Navega a FAGLL03, selecciona "Partidas compensadas", ejecuta la
        consulta y descarga el resultado completo a Excel.

        Devuelve `None` (sin lanzar excepción) si SAP indica que no hay
        resultados para los criterios dados; devuelve la ruta del Excel
        descargado en caso contrario.
        """
        logger.info(
            f"Iniciando generación de reporte de partidas compensadas | "
            f"report_name={report_name}"
        )

        try:
            self._navigate_to_fagll03()
            self._fill_company_code()
            self._fill_accounts()
            self._select_cleared_items()
            self._fill_clearing_date_range()
            self._execute()

            if not self._wait_for_results():
                logger.info(
                    "SAP no reportó partidas compensadas para los criterios dados"
                )
                return None

            output_path = self._download(report_name)

            logger.info(
                f"Reporte de partidas compensadas descargado correctamente | "
                f"path={output_path}"
            )
            return output_path

        except PlaywrightTimeoutError as e:
            logger.exception(f"Timeout generando partidas compensadas: {e}")
            error = RuntimeError(f"Timeout generando partidas compensadas: {e}")
            self._record_error("generate", error)
            raise error from e

        except RuntimeError as e:
            logger.exception(f"RuntimeError generando partidas compensadas: {e}")
            self._record_error("generate", e)
            raise

        except Exception as e:
            logger.exception(f"Error inesperado generando partidas compensadas: {e}")
            error = RuntimeError(f"Error inesperado: {e}")
            self._record_error("generate", error)
            raise error from e

    # ------------------------------------------------------------------
    # SELECCIÓN "PARTIDAS COMPENSADAS" + RANGO DE FECHA
    # ------------------------------------------------------------------

    def _select_cleared_items(self):
        """
        Marca el radio "Partidas compensadas" en la pantalla de
        selección de FAGLL03.
        """
        logger.info("Seleccionando radio 'Partidas compensadas'")

        try:
            cleared_items_radio = self.page.get_by_role(
                "radio", name="Partidas compensadas"
            )
            cleared_items_radio.wait_for(state="visible", timeout=10_000)
            cleared_items_radio.check()

            logger.info("Radio 'Partidas compensadas' seleccionado")

        except Exception as e:
            logger.exception(f"Error seleccionando 'Partidas compensadas': {e}")
            raise

    def _fill_clearing_date_range(self):
        """
        Diligencia el rango "Fecha de compensación" (siempre
        `self.date - 1 mes` a `self.date`, según la regla de negocio).
        """
        low_date = self.date - pd.DateOffset(
            months=self.config.cleared_items_lookback_months
        )
        high_date = self.date

        logger.info(
            "Ingresando rango de fecha de compensación | "
            f"desde={low_date.strftime('%d.%m.%Y')} | "
            f"hasta={high_date.strftime('%d.%m.%Y')}"
        )

        try:
            date_fields = self.page.locator(
                "input[title='Fecha de la compensación']"
            )

            low_field = date_fields.nth(0)
            low_field.wait_for(state="visible", timeout=10_000)
            low_field.fill(low_date.strftime("%d.%m.%Y"))

            high_field = date_fields.nth(1)
            high_field.wait_for(state="visible", timeout=10_000)
            high_field.fill(high_date.strftime("%d.%m.%Y"))

            logger.info("Rango de fecha de compensación diligenciado")

        except Exception as e:
            logger.exception(f"Error ingresando rango de fecha de compensación: {e}")
            raise

    # ------------------------------------------------------------------
    # DETECCIÓN DE RESULTADOS / SIN DATOS
    # ------------------------------------------------------------------

    def _wait_for_results(self) -> bool:
        """
        Espera a que la tabla de resultados cargue. Si no aparece dentro
        del timeout, revisa si SAP mostró el mensaje de "sin datos" antes
        de considerarlo un error real.

        Devuelve `True` si la tabla cargó (hay resultados) y `False` si
        SAP indicó que no hay partidas compensadas para los criterios
        dados.
        """
        logger.info("Esperando resultados de partidas compensadas")

        try:
            self.page.locator("table[id^='userarealist']").first.wait_for(
                state="visible", timeout=60_000
            )
            self.page.wait_for_load_state("networkidle")
            logger.info("Tabla de partidas compensadas renderizada")
            return True

        except PlaywrightTimeoutError:
            no_data = self.page.get_by_text(self.NO_DATA_MESSAGE, exact=False)

            if no_data.count() > 0:
                logger.info("Mensaje de 'sin datos' detectado en SAP")
                return False

            logger.exception(
                "Ni la tabla de resultados ni el mensaje de 'sin datos' "
                "aparecieron a tiempo"
            )
            raise RuntimeError(
                "La lista de partidas compensadas no se renderizó y no se "
                "detectó el mensaje de 'sin datos'."
            )