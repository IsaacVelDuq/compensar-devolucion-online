from pathlib import Path
import logging
import time

import pandas as pd
from .sap_config import SAPGeneralConfig
from .sap_error_recorder import ensure_error_columns, record_error
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)


class SAPConnection:

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        sap_errors_df: pd.DataFrame | None = None,
        user_data_dir: Path = Path("./perfil"),
        df_errores: pd.DataFrame | None = None,
        config: SAPGeneralConfig | None = None,
    ):
        logger.info("Inicializando SAPConnection")

        self.url = url
        self.username = username
        self.password = password
        self.user_data_dir = user_data_dir
        errors_df = df_errores if df_errores is not None else sap_errors_df
        self.sap_errors_df = ensure_error_columns(errors_df)
        self.df_errores = self.sap_errors_df
        self.config = config or SAPGeneralConfig()

        self._playwright = None
        self._context = None
        self._page = None

        logger.info(
            "SAPConnection inicializada | URL: %s | Perfil: %s",
            self.url,
            self.user_data_dir,
        )

    def start(self):
        """
        Inicia Playwright, obtiene la página disponible y realiza el login.
        """
        logger.info("Iniciando Playwright")

        try:
            self._playwright = sync_playwright().start()

            logger.info("Lanzando contexto persistente de Chromium")

            self._context = (
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    channel=self.config.chrome_channel,
                    headless=self.config.headless,
                    accept_downloads=True,
                    permissions=[
                        "clipboard-read",
                        "clipboard-write",
                    ],
                )
            )

            logger.info("Contexto Chromium iniciado correctamente")

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()

            logger.info("Página activa vinculada exitosamente")

            self._login()

            logger.info("Sesión SAP iniciada correctamente")

            return self.get_page()

        except Exception as e:
            logger.exception(
                "Error iniciando SAPConnection: %s",
                e,
            )
            error = Exception(f"Error iniciando conexión en SAP\n{e}")
            self._record_error("start", error)
            raise error from e

    def restart_page(self):
        """
        Reinicia el contexto de SAP manteniendo la instancia de Playwright.
        """
        logger.info(
            "Reiniciando contexto SAP (Playwright se mantiene)"
        )

        try:
            if self._context:
                try:
                    self._context.close()
                except Exception as context_error:
                    logger.warning(
                        "Error al cerrar el contexto anterior: %s",
                        context_error,
                    )
                finally:
                    self._context = None
                    self._page = None

            self._context = (
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    channel=self.config.chrome_channel,
                    headless=self.config.headless,
                    accept_downloads=True,
                    permissions=[
                        "clipboard-read",
                        "clipboard-write",
                    ],
                )
            )

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()

            self._login()

            logger.info(
                "Contexto SAP reiniciado correctamente"
            )

            return self.get_page()

        except Exception as e:
            logger.exception(
                "Error reiniciando contexto SAP: %s",
                e,
            )
            error = Exception(f"Error reiniciando conexión en SAP\n{e}")
            self._record_error("restart_page", error)
            raise error from e

    def _login(self):
        """
        Realiza el inicio de sesión en el portal SAP.
        """
        logger.info("Ingresando al portal SAP")

        try:
            self._page.goto(self.url)
            logger.info("Página SAP cargada")

            self._page.fill(
                "input[name='sap-user']",
                self.username,
            )
            logger.info("Usuario ingresado")

            self._page.fill(
                "input[name='sap-password']",
                self.password,
            )
            logger.info("Contraseña ingresada")

            self._page.keyboard.press("Enter")
            logger.info("Intentando autenticación")

            self._page.wait_for_load_state("networkidle")

            self._wait_for_login_result()

            logger.info("Login exitoso en SAP")

        except InvalidCredentialsError:
            logger.exception("Credenciales incorrectas")

            raise Exception(
                "No fue posible iniciar sesión en SAP. "
                "Actualice el usuario y la contraseña desde "
                "Configuración e inténtelo nuevamente."
            )

        except PlaywrightTimeoutError as e:
            logger.exception(
                "Timeout durante login SAP: %s",
                e,
            )

            raise Exception(
                "Error reiniciando sesión en SAP. "
                "El login no apareció correctamente; "
                "tal vez la conexión a internet es mala."
            )

        except Exception as e:
            logger.exception(
                "Error inesperado durante login SAP: %s",
                e,
            )

            raise Exception(
                f"No se pudo navegar correctamente a la "
                f"ventana principal de SAP\n{e}"
            )

    def _wait_for_login_result(self, timeout=10):
        """
        Espera hasta determinar si el login fue exitoso
        o falló debido a credenciales inválidas.
        """
        sap_image = self._page.get_by_role(
            "img",
            name="Imagen de acceso SAP Easy",
        )

        error_message = self._page.get_by_text(
            "Mandante, nombre o clave no son correctos. "
            "Repita los datos de acceso.",
            exact=True,
        )

        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:

            if sap_image.is_visible():
                return

            if error_message.is_visible():
                raise InvalidCredentialsError(
                    "Usuario o contraseña incorrectos."
                )

            self._page.wait_for_timeout(100)

        raise PlaywrightTimeoutError(
            "No fue posible determinar el resultado del login."
        )

    def get_page(self):
        """
        Devuelve la página activa de SAP.
        """
        logger.debug("Retornando página activa de SAP")
        return self._page

    def close(self):
        """
        Cierra el contexto de SAP y la instancia de Playwright.
        """
        logger.info("Cerrando conexión SAP completa")

        try:
            if self._context:
                self._context.close()
                self._context = None

            if self._playwright:
                self._playwright.stop()
                self._playwright = None

        except Exception as e:
            logger.error(
                "Error cerrando conexión SAP: %s",
                e,
            )

    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def navigate_home(self):
        """
        Regresa al home de SAP.
        """
        logger.info("Navegando al home")

        try:
            ok_field = self._page.locator(
                "input[id='ToolbarOkCode']"
            )

            ok_field.wait_for(
                state="visible",
                timeout=30_000,
            )

            ok_field.click(timeout=2_000)
            ok_field.fill("/n")

            self._page.keyboard.press("Enter")
            self._page.wait_for_load_state("networkidle")
            self._page.wait_for_timeout(1500)

            logger.info("Home cargado correctamente")

        except Exception as e:
            logger.exception(
                "Error navegando a Home: %s",
                e,
            )

            raise Exception(
                f"No se pudo navegar correctamente a la "
                f"ventana principal de SAP\n{e}"
            )

    def get_errors(self):
        """
        Devuelve los errores registrados durante la ejecución en SAP.
        """
        return self.sap_errors_df

    def _record_error(self, operation: str, error: BaseException) -> None:
        record_error(self.df_errores, self.__class__.__name__, operation, error)


class InvalidCredentialsError(Exception):
    """Se lanza cuando las credenciales de SAP son inválidas."""
    pass