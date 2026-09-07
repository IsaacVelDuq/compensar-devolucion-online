from pathlib import Path
import logging
import time

from .sap_config import SAPGeneralConfig
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
        user_data_dir: Path = Path("./perfil"),
        config: SAPGeneralConfig | None = None,
        zoom: str = "67%",
    ):
        logger.info("Inicializando SAPConnection")

        self.url = url
        self.username = username
        self.password = password
        self.user_data_dir = user_data_dir
        self.config = config or SAPGeneralConfig()
        # Zoom de página real (mismo mecanismo que Ctrl+- en Chrome), NO un
        # factor de escala de dispositivo: la página reacomoda su layout
        # para llenar el espacio disponible en vez de dejar franjas en
        # blanco. Se aplica vía add_init_script en _launch_context, y
        # ADEMÁS se reaplica en cada frameattached/framenavigated, porque
        # SAP WebGUI repuebla varios de sus iframes internos sin disparar
        # una navegación "completa" que Playwright reconozca — si solo
        # dependiéramos del init_script, esos frames se quedarían sin
        # zoom o lo perderían tras el primer refresh.
        self.zoom = zoom

        self._playwright = None
        self._context = None
        self._page = None

        logger.info(
            "SAPConnection inicializada | URL: %s | Perfil: %s | Zoom: %s",
            self.url,
            self.user_data_dir,
            self.zoom,
        )

    # ------------------------------------------------------------------
    # UTILIDADES DE ARRANQUE / ESTABILIDAD DEL PERFIL Y DESCARGAS
    # ------------------------------------------------------------------

    def _clear_stale_lock(self):
        """
        Elimina archivos de bloqueo residuales de una sesión previa que
        no haya cerrado limpiamente (crash, kill manual, dos instancias
        corriendo sobre el mismo perfil, etc.). Se ejecuta ANTES de cada
        launch_persistent_context, sin depender de que el cierre anterior
        haya sido correcto.
        """
        lock_files = [
            "SingletonLock",
            "SingletonSocket",
            "SingletonCookie",
        ]

        for name in lock_files:
            lock_path = self.user_data_dir / name

            if lock_path.exists():
                try:
                    lock_path.unlink()
                    logger.warning(
                        "Eliminado archivo de bloqueo residual: %s",
                        lock_path,
                    )
                except Exception as e:
                    logger.warning(
                        "No se pudo eliminar %s: %s",
                        lock_path,
                        e,
                    )

    def _ensure_download_behavior(self):
        """
        Reafirma explícitamente vía CDP que las descargas se permiten
        sin diálogo nativo del sistema operativo, y hacia dónde. Se llama
        al arrancar la sesión y también debe llamarse antes de cada
        operación de descarga individual, para no depender de que el
        permiso se mantenga estable solo por estar en un perfil ya usado
        (se observó el patrón: primera descarga OK, segunda descarga
        falla / cierra el browser).
        """
        download_dir = getattr(self.config, "download_dir", None)

        if download_dir is None:
            logger.debug(
                "config.download_dir no está definido; se omite "
                "Page.setDownloadBehavior explícito"
            )
            return

        try:
            cdp_session = self._context.new_cdp_session(self._page)
            cdp_session.send(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(download_dir),
                },
            )
            logger.info("Comportamiento de descarga CDP configurado")
        except Exception as e:
            logger.warning(
                "No se pudo configurar comportamiento de descarga vía CDP: %s",
                e,
            )

    # ------------------------------------------------------------------
    # ZOOM — aplicación e infraestructura de reintento por frame
    # ------------------------------------------------------------------

    def _zoom_script(self) -> str:
        """Script inyectable que fija el zoom de página en el documento actual."""
        return f'document.documentElement.style.zoom = "{self.zoom}";'

    def _reapply_zoom(self, frame):
        """
        Reaplica el zoom sobre un frame específico. Se usa como callback
        de frameattached/framenavigated, así que debe tolerar frames que
        ya se destruyeron antes de que el evaluate alcance a correr
        (navegación rapidísima, frame descartado, etc.) sin propagar la
        excepción y sin tumbar el listener.
        """
        try:
            frame.evaluate(self._zoom_script())
        except Exception as e:
            logger.debug(
                "No se pudo reaplicar zoom en frame (probablemente ya "
                "destruido): %s",
                e,
            )

    def _bind_zoom_listeners(self, page):
        """
        Engancha los listeners de reaplicación de zoom sobre una página.
        Se llama tanto para páginas nuevas del contexto como para las que
        ya existían al momento de crear el contexto.
        """
        page.on("frameattached", self._reapply_zoom)
        page.on("framenavigated", self._reapply_zoom)

#, chrome_channel: str | None
    def _launch_context(self, headless: bool):
        """
        Lanza el contexto persistente de Chromium con la configuración
        estándar (limpieza de locks previa incluida). Centralizado acá
        para que start() y restart_page() no diverjan.
        """
        self._clear_stale_lock()
        window_args = [
            "--start-maximized",
        ]
        if not headless:
            screen_size = self._screen_size()
            if screen_size:
                width, height = screen_size
                window_args.extend(
                    [
                        f"--window-position=0,0",
                        f"--window-size={width},{height}",
                    ]
                )

        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            #channel=chrome_channel,
            headless=headless,
            viewport=None,
            accept_downloads=True,
            permissions=[
                "clipboard-read",
                "clipboard-write",
            ],
            args=window_args,
        )

        # Se re-ejecuta automáticamente en CADA documento y CADA iframe, en
        # cada navegación futura de este contexto (incluida la primera
        # navegación de la página ya abierta al crear el contexto) —
        # cubre los frames internos de SAP WebGUI sin tener que reenganchar
        # listeners manualmente cada vez que se llama restart_page().
        #
        # `document.documentElement.style.zoom` es zoom de página real de
        # Chromium: el mismo mecanismo de Ctrl+-. El contenido se reacomoda
        # para llenar el ancho disponible, a diferencia de un
        # --force-device-scale-factor, que reescala el renderizado sin que
        # la página sepa que tiene más espacio y por eso deja franjas en
        # blanco.
        context.add_init_script(self._zoom_script())

        # Red de seguridad: SAP WebGUI repuebla varios de sus iframes
        # internos (toolbar, grid, status bar) sin disparar siempre una
        # navegación "completa" de documento. En esos casos el
        # add_init_script de arriba puede no llegar a tiempo o no
        # disparar en absoluto. Reaplicamos el zoom explícitamente cada
        # vez que Playwright detecta un frame nuevo o navegado.
        context.on("page", self._bind_zoom_listeners)
        for existing_page in context.pages:
            self._bind_zoom_listeners(existing_page)

        return context

    @staticmethod
    def _screen_size() -> tuple[int, int] | None:
        """Obtiene el tamaño de pantalla en Windows para reforzar el launch."""
        if not hasattr(__import__("sys"), "getwindowsversion"):
            return None

        import ctypes

        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    # ------------------------------------------------------------------
    # CICLO DE VIDA
    # ------------------------------------------------------------------

    def start(self):
        """
        Inicia Playwright, obtiene la página disponible y realiza el login.
        """
        logger.info("Iniciando Playwright")

        try:
            self._playwright = sync_playwright().start()

            logger.info("Lanzando contexto persistente de Chromium")

            self._context = self._launch_context(
                headless=False,
                #chrome_channel=self.config.chrome_channel,
            )

            logger.info("Contexto Chromium iniciado correctamente")

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()

            logger.info("Página activa vinculada exitosamente")

            self._ensure_download_behavior()

            self._login()

            logger.info("Sesión SAP iniciada correctamente")

            return self.get_page()

        except Exception as e:
            logger.exception(
                "Error iniciando SAPConnection: %s",
                e,
            )
            error = Exception(f"Error iniciando conexión en SAP\n{e}")
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

            self._context = self._launch_context(
                headless=self.config.headless,
                #chrome_channel=self.config.chrome_channel,
            )

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()

            self._ensure_download_behavior()

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
                try:
                    self._context.close()
                except Exception as e:
                    logger.error("Error cerrando contexto SAP: %s", e)
                finally:
                    self._context = None
        finally:
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception as e:
                    logger.error("Error deteniendo Playwright: %s", e)
                finally:
                    self._playwright = None

    # ------------------------------------------------------------------
    # CONTEXT MANAGER — garantiza close() incluso si algo falla en medio
    # del procesamiento, para no dejar el perfil en estado inconsistente
    # de cara a la próxima ejecución.
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

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


class InvalidCredentialsError(Exception):
    """Se lanza cuando las credenciales de SAP son inválidas."""
    pass