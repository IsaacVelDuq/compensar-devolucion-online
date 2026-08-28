from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import logging
import datetime
import pandas as pd
import re

logger = logging.getLogger(__name__)


# =====================================================================
# CLASE BASE: COMPENSACIÓN F-03
# =====================================================================

class CompensarF03():
    """
    Automatiza la compensación de cuentas por cobrar en SAP mediante la
    transacción F-03 (Compensar partidas abiertas de deudor).

    Attributes:
        page: Instancia de Playwright Page activa.
    """

    def __init__(self, page: object):
        self.page = page


    # ------------------------------------------------------------------
    # NAVEGACIÓN
    # ------------------------------------------------------------------

    def _navigate_to_f03(self):
        """
        Navega a la transacción F-03 desde cualquier pantalla de SAP
        usando el campo ToolbarOkCode con el comando /nF-03.

        Raises:
            PlaywrightTimeoutError: Si el campo de transacción no carga a tiempo.
            Exception: Si ocurre cualquier otro error durante la navegación.
        """
        logger.info("Navegando a transacción F-03")

        try:
            ok_field = self.page.locator("input[id='ToolbarOkCode']")
            ok_field.wait_for(state="visible", timeout=20_000)
            ok_field.click(timeout=2_000)
            ok_field.fill("/nF-03")
            self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("networkidle")
            logger.info("Transacción F-03 cargada correctamente")

        except PlaywrightTimeoutError:
            logger.exception("Timeout esperando campo de transacción en F-03")
            raise Exception("Tiempo de espera agotado esperando campo de transacción en F-03")

        except Exception:
            logger.exception("Error navegando a F-03")
            raise Exception("Error navegando a F-03")


    # ------------------------------------------------------------------
    # LLENADO DE CAMPOS INICIALES
    # ------------------------------------------------------------------

    def _fill_fields(self, sociedad: str, cuenta: str, moneda: str, fecha: datetime.datetime):
        """
        Rellena los campos del formulario inicial de F-03:
        cuenta contable, sociedad, moneda y fecha de compensación.
        Al finalizar selecciona el criterio por Nº documento y hace clic
        en 'Tratar PAs' para cargar las partidas abiertas.

        Args:
            sociedad : Código de sociedad SAP (p.ej. '1000').
            cuenta   : Número de cuenta contable del deudor.
            moneda   : Moneda del documento (p.ej. 'COP', 'USD').
            fecha    : Fecha de compensación a registrar.

        Raises:
            PlaywrightTimeoutError: Si algún campo no carga a tiempo.
            Exception: Si ocurre cualquier otro error al llenar los campos.
        """
        logger.info(
            "Llenando campos F-03 | sociedad=%s | cuenta=%s | fecha=%s",
            sociedad, cuenta, fecha.strftime("%d.%m.%Y")
        )

        try:
            # ── Cuenta contable ──────────────────────────────────────────
            cuenta_field = self.page.get_by_role("textbox", name="Cuenta Necesarios")
            cuenta_field.wait_for(state="visible", timeout=1_500)
            cuenta_field.fill(str(cuenta))
            logger.info("Cuenta ingresada: %s", cuenta)

            # ── Sociedad ─────────────────────────────────────────────────
            sociedad_field = self.page.get_by_role("textbox", name="Sociedad Necesarios")
            sociedad_field.wait_for(state="visible", timeout=1_500)
            sociedad_field.fill(str(sociedad))
            logger.info("Sociedad ingresada: %s", sociedad)

            # ── Moneda ───────────────────────────────────────────────────
            moneda_field = self.page.get_by_role("textbox", name="Moneda")
            moneda_field.wait_for(state="visible", timeout=1_500)
            moneda_field.fill(str(moneda))
            logger.info("Moneda ingresada: %s", moneda)

            # ── Fecha de compensación ────────────────────────────────────
            fecha_field = self.page.get_by_role("textbox", name="Fe.compensación Necesarios")
            fecha_field.wait_for(state="visible", timeout=1_500)
            fecha_field.fill(fecha.strftime("%d.%m.%Y"))
            logger.info("Fecha ingresada: %s", fecha.strftime("%d.%m.%Y"))

            texto = self._detectar_dialogo("Período contable")
            if texto:
                error = pd.DataFrame([{
                    "Error en Transacción": "f-03",
                    "Descripción": f"{texto}"
                    }])
                self.df_errores_sap = pd.concat([self.df_errores_sap,error], ignore_index=True)
                raise Exception(f"Diálogo inesperado detectado tras ingresar fecha en F-03: \n {texto}")

            # ── Criterio de búsqueda: Nº documento ───────────────────────
            self.page.get_by_role("radio", name="Nº documento").check()
            logger.info("Radio 'Nº documento' seleccionado")

            # ── Lanzar búsqueda de partidas abiertas ─────────────────────
            self.page.get_by_role("button", name="Tratar PAs").click(timeout=2_000)
            logger.info("Botón 'Tratar PAs' presionado — esperando carga de partidas")

            texto = self._detectar_dialogo("Período contable")
            if texto:
                raise Exception(f"Diálogo inesperado detectado tras presionar 'Tratar PAs' en F-03: \n{texto}")

        except PlaywrightTimeoutError as e:
            logger.exception("Timeout llenando campos de F-03")
            raise Exception(f"Tiempo de espera agotado llenando campos de F-03: \n{e}")

        except Exception as e:
            logger.exception("Error llenando campos de F-03")
            raise Exception((f"Error llenando campos de F-03: \n{e}"))


    def _wait_for_loading(self):
        self.page.wait_for_timeout(1500)
        self.page.wait_for_load_state("networkidle")
        try:
            self.page.locator("#ur-loading-footer").wait_for(
                state="visible",
                timeout=5_000
            )
            self.page.locator("#ur-loading-footer").wait_for(
                state="hidden",
                timeout=300_000
            )
        except Exception:
            logger.debug("No se detectó indicador de carga, continuando...")



    # ------------------------------------------------------------------
    # DETECCIÓN DE DIÁLOGOS SAP
    # ------------------------------------------------------------------

    def _detectar_dialogo(self, texto_esperado: str, doc_creado: bool = False) -> str | None:
        """
        Espera, valida y cierra un diálogo modal de SAP.

        Intenta hasta 4 veces (cada 500 ms) antes de determinar que el diálogo
        no está presente, compensando los tiempos de renderizado de SAP Web GUI.

        Args:
            texto_esperado: Fragmento que debe aparecer en el diálogo para
                            considerarlo válido y cerrarlo.
            doc_creado:     Si True, extrae el número de documento SAP del patrón
                            "Doc.<número>" antes de retornar.

        Returns:
            - Si ``doc_creado=True``:
                - El número de documento como str (ej. ``"3803645132"``), o
                - ``" "`` (espacio) si el diálogo fue válido pero no contenía número
                — preserva el valor truthy del retorno para el caller.
            - Si ``doc_creado=False``:
                - El texto completo del diálogo si ``texto_esperado`` estaba presente.
            - ``None`` si el diálogo no era visible o no contenía ``texto_esperado``.

        Note:
            El caller puede distinguir "diálogo detectado sin número" de "no detectado"
            con: ``if resultado and resultado.strip()``.

        Raises:
            Exception: Si ocurre un error inesperado al inspeccionar el DOM.
        """
        try:
            dialogo = self.page.locator("div[role='dialog']")

            # SAP Web GUI puede tardar varios ciclos en renderizar el diálogo modal;
            # se espera hasta 2 s en total antes de desistir.
            for _ in range(4):
                if dialogo.is_visible():
                    break
                self.page.wait_for_timeout(500)

            if not dialogo.is_visible():
                return None  # El diálogo nunca apareció

            texto = dialogo.inner_text()
            logger.info("Diálogo detectado: %s", texto)

            if texto_esperado not in texto:
                # El diálogo existe pero no es el que esperábamos (ej. otro error de SAP)
                return None

            logger.info("Diálogo esperado confirmado: '%s'", texto_esperado)

            # Dar un breve margen antes de cerrar para que SAP termine de renderizar
            self.page.wait_for_timeout(500)
            self.page.get_by_role("button", name="OK").click(timeout=2_000)
            self.page.wait_for_load_state("networkidle")

            if not doc_creado:
                return texto

            # Extraer número de documento del patrón "Doc.XXXXXXXXXX"
            match = re.search(r"Doc\.(\d+)", texto)
            if match:
                return match.group(1)

            # Diálogo válido pero sin número de documento (ej. compensación sin contabilización);
            # retornamos " " en lugar de None para mantener el valor truthy en el caller.
            return " "

        except Exception:
            logger.exception("Error inspeccionando diálogo modal")
            raise


    # ── Wrappers semánticos sobre _detectar_dialogo ──────────────────────

    def _partidas_tomadas(self) -> bool:
        """
        Detecta el diálogo 'Se grabaron los datos. Pueden entrarse más valores.'

        Aparece cuando SAP procesa con éxito un lote de documentos y está listo
        para recibir el siguiente.

        Returns:
            True  → Diálogo detectado y cerrado.
            False → Diálogo no presente.
        """
        texto_esperado = "Se grabaron los datos. Pueden entrarse más valores."
        logger.info("Verificando diálogo de confirmación de lote: '%s'", texto_esperado)
        return self._detectar_dialogo(texto_esperado)

    def _no_partidas_abiertas(self) -> bool:
        """
        Detecta el diálogo 'No se encontró ninguna posición de documento adecuada.'

        Indica que los documentos ingresados no tienen partidas abiertas disponibles.

        Returns:
            True  → Diálogo detectado y cerrado (no hay partidas).
            False → Diálogo no presente (hay partidas disponibles).
        """
        texto_esperado = "No se encontró ninguna posición de documento adecuada."
        logger.info("Verificando diálogo de partidas no encontradas: '%s'", texto_esperado)
        return self._detectar_dialogo(texto_esperado)

    def _partidas_seleccionadas(self) -> bool:
        """
        Detecta el diálogo informativo que contiene 'partidas seleccionadas'.

        Aparece tras seleccionar partidas para informar cuántas fueron marcadas.

        Returns:
            True  → Diálogo detectado y cerrado.
            False → Diálogo no presente.
        """
        texto_esperado = "partidas seleccionadas"
        logger.info("Verificando diálogo de resumen de selección: '%s'", texto_esperado)
        return self._detectar_dialogo(texto_esperado)

    def _compensacion_creada(self) -> bool:
        """
        Detecta el diálogo 'se contabilizó en sociedad'.

        Confirma que la partida de compensación fue creada exitosamente en SAP.

        Returns:
            True  → Diálogo detectado y cerrado (compensación creada).
            False → Diálogo no presente (posible error en creación).
        """
        texto_esperado = "se contabilizó en sociedad"
        logger.info("Verificando diálogo de confirmación de compensación: '%s'", texto_esperado)
        self.page.wait_for_timeout(1_500)
        self.page.wait_for_load_state("networkidle")
        return self._detectar_dialogo(texto_esperado,True)


    # ------------------------------------------------------------------
    # LLENADO DE DOCUMENTOS
    # ------------------------------------------------------------------

    def _llenar_documentos(self, documentos: list) -> bool:
        """
        Ingresa números de documento en los campos 'De' de la tabla de
        partidas abiertas en F-03.

        SAP renderiza un número limitado de filas según el zoom del navegador.
        Cuando se llenan todos los campos visibles se presiona Enter para que
        SAP procese el lote y habilite más campos. El ciclo se repite hasta
        agotar la lista completa de documentos.

        Nota sobre índices: SAP incluye un campo de cabecera 'De' que ocupa
        nth(0), por lo que los campos editables comienzan en nth(1). La variable
        `aux` representa siempre el índice del campo editable actual.

        Args:
            documentos: Lista de strings con los números de documento a ingresar.

        Returns:
            True → Todos los documentos fueron ingresados correctamente.

        Raises:
            RuntimeError: Si SAP reporta que no hay partidas abiertas o si no
                          se encuentran campos 'De' disponibles.
            PlaywrightTimeoutError: Si un campo no responde en el tiempo esperado.
            Exception: Para cualquier otro error inesperado.
        """
        logger.info("Iniciando llenado de documentos | total_documentos=%d", len(documentos))

        try:
            # Esperar a que SAP finalice la carga de la pantalla de partidas
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(1_500)

            # Contar campos editables disponibles (se resta 1 por la cabecera)
            # Hasta 4 reintentos de 500 ms si SAP aún no los ha renderizado
            total_campos = self.page.get_by_role("textbox", name="De").count() - 1
            for _ in range(4):
                if total_campos <= 0:
                    self.page.wait_for_timeout(500)
                    total_campos = self.page.get_by_role("textbox", name="De").count() - 1
                else:
                    break

            logger.info("Campos 'De' editables renderizados por SAP: %d", total_campos)

            if total_campos <= 0:
                raise RuntimeError("No se encontraron campos 'De' disponibles que indica el textbox para insertar numero de documentos en F-03.")

            aux = 1        # nth(1) = primer campo editable (nth(0) es la cabecera)
            n   = len(documentos)

            for i in range(n):
                self.page.wait_for_timeout(300)

                field = self.page.get_by_role("textbox", name="De").nth(aux)

                try:
                    # Hasta 4 reintentos de 500 ms si el campo aún no es visible
                    field.wait_for(state="visible", timeout=500)
                    for _ in range(4):
                        if not field.is_visible():
                            self.page.wait_for_timeout(500)
                        else:
                            break

                    field.fill(documentos[i])
                    logger.info(
                        "Documento ingresado | pos=%d | doc=%s | progreso=%d/%d",
                        aux, documentos[i], i + 1, n
                    )

                except PlaywrightTimeoutError:
                    logger.exception(
                        "Timeout esperando campo 'De' en posición %d para documento %s",
                        aux, documentos[i]
                    )
                    raise Exception(f"Tiempo de espera agotado esperando campo que indica el textbox para insertar numero de documentos {documentos[i]}")

                # ── Último documento: cerrar el formulario ───────────────
                if i == n - 1:
                    logger.info("Último documento ingresado — presionando 'Tratar PAs' para finalizar")
                    self.page.get_by_role("button", name="Tratar PAs").click(timeout=2_000)
                    self.page.wait_for_load_state("networkidle")

                    if self._no_partidas_abiertas():
                        raise RuntimeError("SAP reportó que no hay partidas abiertas para compensar.")

                # ── Lote completo: enviar a SAP y preparar siguiente lote ─
                elif aux == total_campos:
                    logger.info("Lote completo (%d campos) — enviando a SAP con Enter", total_campos)
                    self.page.keyboard.press("Enter")
                    self.page.wait_for_load_state("networkidle")
                    self.page.wait_for_timeout(500)

                    if self._no_partidas_abiertas():
                        raise RuntimeError("SAP reportó que no hay partidas abiertas para compensar.")

                    if self._partidas_tomadas():
                        # SAP limpió los campos; reiniciar índice para el siguiente lote
                        logger.info("Lote grabado — reiniciando índice de campos para el siguiente lote")
                        aux = 1
                        continue

                # ── Avanzar al siguiente campo dentro del lote actual ────
                else:
                    aux += 1

            logger.info("Llenado de documentos completado exitosamente | total_ingresados=%d", n)
            return True

        except PlaywrightTimeoutError:
            logger.exception("Timeout durante el llenado de documentos")
            raise Exception("Timeout durante el llenado de documentos")

        except RuntimeError:
            raise Exception("Error inesperado durante el llenado de documentos")

        except Exception:
            logger.exception("Error inesperado durante el llenado de documentos")
            raise Exception("Error inesperado durante el llenado de documentos")


    # ------------------------------------------------------------------
    # APERTURA DE FORMULARIO 'ELIMINAR DIFERENCIAS'
    # ------------------------------------------------------------------

    def _abrir_eliminar_diferencias(self):
        """
        Cierra el diálogo de partidas seleccionadas (si está presente) y
        hace clic en el botón 'Eliminar diferencias' para abrir su formulario.
        """
        logger.info("Abriendo formulario 'Eliminar diferencias' tras selección de partidas")

        self.page.wait_for_load_state("networkidle")
        self._partidas_seleccionadas()
        self.page.wait_for_load_state("networkidle")

        eliminar_diferencias_btn = self.page.get_by_role("button", name="Eliminar diferencias")
        eliminar_diferencias_btn.wait_for(state="visible", timeout=1_500)
        eliminar_diferencias_btn.click(timeout=2_000)
        logger.info("Formulario 'Eliminar diferencias' abierto")
        self.page.wait_for_load_state("networkidle")


# =====================================================================
# SUBCLASE: CREACIÓN DE CUENTAS POR COBRAR (DOGAMA)
# =====================================================================

class CrearCXC(CompensarF03):
    """
    Extiende CompensarF03 para el flujo específico de creación de CxC de DOGAMA.

    Crea partidas abiertas con importe positivo en F-03 y las compensa contra
    los pagos registrados, actualizando los DataFrames de partidas y pagos.

    Attributes:
        page            : Instancia de Playwright Page activa.
        df_compensar    : DataFrame con los grupos de documentos a compensar.
        df_pagos_dogama : DataFrame con los pagos de DOGAMA.
        partidas        : DataFrame maestro de partidas abiertas.
    """

    def __init__(
        self,
        page: object,
        df_compensar: pd.DataFrame,
        df_pagos_dogama: pd.DataFrame,
        partidas: pd.DataFrame,
        df_procesadas_compensadas : pd.DataFrame,
        df_errores_sap: pd.DataFrame
    ):
        self.page            = page
        self.df_compensar    = df_compensar
        self.df_pagos_dogama = df_pagos_dogama
        self.partidas        = partidas
        self.df_procesadas_compensadas = df_procesadas_compensadas
        self.df_errores_sap = df_errores_sap


    # ------------------------------------------------------------------
    # ELIMINACIÓN DE DIFERENCIAS (override)
    # ------------------------------------------------------------------

    def _eliminar_diferencias(self, referencia: str, texto: str, clase_documento: str, BP: str):
        """
        Rellena el formulario 'Eliminar diferencias' con los datos del ajuste
        de DOGAMA y confirma con 'Tratar PAs'.

        Args:
            referencia      : Texto de referencia del documento de ajuste.
            texto           : Descripción para la cabecera del documento (Txt.cab.doc.).
            clase_documento : Clave de clase de documento SAP (ClvCT).
            BP              : Número de cuenta del business partner.
        """
        logger.info(
            "Iniciando eliminación de diferencias | referencia=%s | clase=%s | BP=%s",
            referencia, clase_documento, BP
        )

        self._abrir_eliminar_diferencias()

        # ── Referencia ───────────────────────────────────────────────────
        referencia_txt = self.page.get_by_role("textbox", name="Referencia")
        referencia_txt.wait_for(state="visible", timeout=1_500)
        referencia_txt.fill(referencia)
        logger.info("Referencia ingresada: %s", referencia)

        # ── Texto de cabecera ────────────────────────────────────────────
        txt_doc = self.page.get_by_role("textbox", name="Txt.cab.doc.")
        txt_doc.wait_for(state="visible", timeout=1_500)
        txt_doc.fill(texto)
        logger.info("Texto de cabecera ingresado: %s", texto)

        # ── Clase de documento (ClvCT) ───────────────────────────────────
        clase_documento_txt = self.page.get_by_role("textbox", name="ClvCT")
        clase_documento_txt.wait_for(state="visible", timeout=1_500)
        clase_documento_txt.fill(clase_documento)
        logger.info("Clase de documento ingresada: %s", clase_documento)

        # ── Cuenta del business partner ──────────────────────────────────
        cuenta = self.page.get_by_role("textbox", name="Cuenta")
        cuenta.wait_for(state="visible", timeout=1_500)
        cuenta.fill(BP)
        logger.info("Cuenta BP ingresada: %s", BP)

        # ── Confirmar y procesar ─────────────────────────────────────────
        btn_tratar_pas = self.page.get_by_role("button", name="Tratar PAs")
        btn_tratar_pas.wait_for(state="visible", timeout=1_500)
        btn_tratar_pas.click(timeout=2_000)
        logger.info("Diferencias enviadas con 'Tratar PAs'")
        self.page.wait_for_load_state("networkidle")


    # ------------------------------------------------------------------
    # REGISTRO DE POSICIÓN DE AJUSTE
    # ------------------------------------------------------------------

    def _compensar(self, valor: str, texto: str, nit: str):
        """
        Registra la posición de ajuste final (importe, NIT y texto) y
        contabiliza el documento con 'Contabilizar Resaltado'.

        Args:
            valor : Importe de la posición (formato SAP: '1234').
            texto : Descripción de la posición.
            nit   : NIT del deudor a registrar en el campo Asignación.
        """
        logger.info("Registrando posición de ajuste | nit=%s | valor=%s", nit, valor)

        self.page.wait_for_load_state("networkidle")

        # ── Importe ──────────────────────────────────────────────────────
        importe_txt = self.page.get_by_role("textbox", name="Importe", exact=True)
        importe_txt.wait_for(state="visible", timeout=1_500)
        importe_txt.fill(valor)
        logger.info("Importe ingresado: %s", valor)

        # ── Asignación (NIT) ─────────────────────────────────────────────
        nit_txt = self.page.get_by_role("textbox", name="Asignación")
        nit_txt.wait_for(state="visible", timeout=1_500)
        nit_txt.fill(nit)
        logger.info("NIT ingresado en Asignación: %s", nit)

        # ── Texto de la posición ─────────────────────────────────────────
        texto_txt = self.page.get_by_role("textbox", name="Texto")
        texto_txt.wait_for(state="visible", timeout=1_500)
        texto_txt.fill(texto)
        logger.info("Texto de posición ingresado: %s", texto)

        # ── Contabilizar ─────────────────────────────────────────────────
        ejecutar = self.page.get_by_role("button", name="Contabilizar  Resaltado")
        texto = self._detectar_dialogo("está en el pasado")
        if texto:
            raise Exception(texto)
        ejecutar.click(timeout=10_000)
        self.page.wait_for_load_state("networkidle")


    # ------------------------------------------------------------------
    # FLUJO PRINCIPAL DE CREACIÓN DE CxC
    # ------------------------------------------------------------------

    def crear_cxc(self, conn) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Itera sobre df_compensar y crea una CxC en SAP por cada fila.

        Por cada fila exitosa actualiza self.partidas (eliminando los documentos
        ya compensados) y self.df_pagos_dogama (registrando los documentos usados).
        Las filas con error se omiten con continue para no interrumpir el lote.

        Args:
            conn: Instancia de SAPConexion para reiniciar la página entre filas.

        Returns:
            Tupla (partidas actualizadas, df_pagos_dogama actualizado).
        """
        logger.info("Iniciando proceso de creación de CxC para DOGAMA")

        for idx, row in self.df_compensar.iterrows():

            # ── Reinicio de página entre iteraciones ─────────────────────
            try:
                logger.info("Reiniciando página SAP para fila %d", idx)
                self.page = conn.restart_page() if idx > 0 else self.page
            except Exception:
                logger.exception("Error al reiniciar la página SAP para fila %d", idx)
                error = pd.DataFrame([{
                    "Error en Transacción": "f-03",
                    "Descripción": f"Error al reiniciar la página SAP para crear cxc a Dogama para {row["Documentos"]} con un valor general {str(int(round(row["Valor"], 0)))}"
                    }])
                self.df_errores_sap = pd.concat([self.df_errores_sap,error], ignore_index=True)
                continue

            # ── Procesamiento de la fila ──────────────────────────────────
            try:
                logger.info("Procesando fila %d: %s", idx, row.to_dict())

                documentos      = row["Documentos"]
                cuenta_mayor    = row["cuenta"]
                sociedad        = row["Sociedad"]
                clave           = row["Número transferencia"]
                moneda          = row["Moneda del documento"]
                #fecha           = row["Fecha"]
                fecha           = datetime.now().strftime("%d/%m/%Y")
                referencia      = "CXC DOGAMA"
                texto           = row["Concepto"]
                clase_documento = "01"
                valor           = str(int(round(row["Valor"], 0)))
                BP              = row["BP_dogama"]
                nit             = row["nit_dogama"]

                self._navigate_to_f03()
                self._fill_fields(sociedad, cuenta_mayor, moneda, fecha)
                self._llenar_documentos(documentos)
                self._eliminar_diferencias(referencia, texto, clase_documento, BP)
                self._compensar(valor, texto, nit)
                self._wait_for_loading()
                doc = self._compensacion_creada()
                if doc: 
                    logger.info("CxC creada exitosamente para fila %d", idx)

                    # Excluir documentos ya compensados del maestro de partidas
                    self.partidas = self._actualizar_partidas(self.partidas, documentos)

                    # Registrar los documentos usados en el df de pagos DOGAMA
                    mask = self.df_pagos_dogama["Número transferencia"] == clave
                    self.df_pagos_dogama.loc[mask, "Documentos"] = ", ".join(map(str, documentos))

                    compensada = {
                        "Documento creado": doc,
                        "Sociedad": sociedad,
                        "Cuenta de mayor": cuenta_mayor,
                        "Proceso": "CXC creada",
                        "Documentos asociados": ", ".join(map(str, documentos))
                    }
                    df_compensada = pd.DataFrame([compensada])  
                    self.df_procesadas_compensadas = pd.concat(
                        [self.df_procesadas_compensadas, df_compensada], 
                        ignore_index=True
                    )
                else:
                    raise RuntimeError(
                        "No se detectó confirmación de creación de CxC para fila %d" % idx
                    )

            except Exception as e :
                logger.exception("Error creando CxC para documentos %s", documentos)
                error = pd.DataFrame([{
                    "Error en Transacción": "f-03",
                    "Descripción": f"{e} para procesar documentos: {documentos} "
                    }])
                self.df_errores_sap = pd.concat([self.df_errores_sap,error], ignore_index=True)
                continue

        return self.partidas, self.df_pagos_dogama, self.df_procesadas_compensadas


    # ------------------------------------------------------------------
    # UTILIDADES
    # ------------------------------------------------------------------

    def _actualizar_partidas(self, partidas: pd.DataFrame, documentos: list) -> pd.DataFrame:
        """Elimina del DataFrame de partidas los documentos ya compensados."""
        for p in documentos:
            partidas = partidas[partidas["Nº documento"] != p]
        return partidas

    def get_errors(self):
        return self.df_errores_sap


# =====================================================================
# SUBCLASE: COMPENSACIÓN DE CONTRAPARTIDAS
# =====================================================================

class CompensarContrapartidas(CompensarF03):
    """
    Extiende CompensarF03 para compensar pares de documentos (partida +
    contrapartida), con soporte para ajuste de diferencias de importe.

    Attributes:
        page         : Instancia de Playwright Page activa.
        df_compensar : DataFrame con los pares de documentos a compensar.
        df_partidas  : Copia del DataFrame maestro de partidas abiertas.
    """

    def __init__(self, page: object, df_compensar: pd.DataFrame, df_partidas: pd.DataFrame,df_procesadas_compensadas,df_errores_sap: pd.DataFrame):
        self.page         = page
        self.df_compensar = df_compensar
        self.df_partidas  = df_partidas.copy()
        self.df_procesadas_compensadas = df_procesadas_compensadas
        self.df_errores_sap = df_errores_sap

    # ------------------------------------------------------------------
    # ELIMINACIÓN DE DIFERENCIAS (override)
    # ------------------------------------------------------------------

    def _eliminar_diferencias(
        self,
        texto: str,
        clase_documento: str,
        cuenta_ajuste: str,
        diff_value: str,
        ind_impuestos: str = "VZ"
    ):
        """
        Rellena el formulario 'Eliminar diferencias' para el ajuste de la
        diferencia de importe entre la partida y su contrapartida.

        El flujo presiona Enter tras ingresar la cuenta de ajuste para que SAP
        exponga los campos de importe e indicador de impuestos.

        Args:
            texto           : Texto de referencia y cabecera del documento de ajuste.
            clase_documento : Clave de clase de documento SAP (ClvCT).
            cuenta_ajuste   : Cuenta contable de ajuste (p.ej. '5395950001').
            diff_value      : Importe de la diferencia a registrar (como string).
            ind_impuestos   : Indicador de impuestos SAP (por defecto 'VZ').
        """
        self.page.wait_for_load_state("networkidle")

        # ── Referencia ───────────────────────────────────────────────────
        referencia_txt = self.page.get_by_role("textbox", name="Referencia")
        referencia_txt.wait_for(state="visible", timeout=1_500)
        referencia_txt.fill(texto)
        logger.info("Referencia ingresada: %s", texto)

        # ── Texto de cabecera ────────────────────────────────────────────
        txt_doc = self.page.get_by_role("textbox", name="Txt.cab.doc.")
        txt_doc.wait_for(state="visible", timeout=1_500)
        txt_doc.fill(texto)
        logger.info("Texto de cabecera ingresado: %s", texto)

        # ── Clase de documento (ClvCT) ───────────────────────────────────
        clase_documento_txt = self.page.get_by_role("textbox", name="ClvCT")
        clase_documento_txt.wait_for(state="visible", timeout=1_500)
        clase_documento_txt.fill(clase_documento)
        logger.info("Clase de documento ingresada: %s", clase_documento)

        # ── Cuenta de ajuste ─────────────────────────────────────────────
        cuenta = self.page.get_by_role("textbox", name="Cuenta")
        cuenta.wait_for(state="visible", timeout=1_500)
        cuenta.fill(cuenta_ajuste)
        logger.info("Cuenta de ajuste ingresada: %s", cuenta_ajuste)

        # ── Enter para que SAP exponga campos de importe e impuestos ─────
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(500)
        self.page.keyboard.press("Enter")
        self.page.wait_for_load_state("networkidle")

        # ── Importe de la diferencia ─────────────────────────────────────
        importe_txt = self.page.get_by_role("textbox", name="Importe")
        importe_txt.wait_for(state="visible", timeout=1_500)
        importe_txt.fill(diff_value)
        logger.info("Importe de diferencia ingresado: %s", diff_value)

        # ── Indicador de impuestos ───────────────────────────────────────
        ind_txt = self.page.get_by_role("textbox", name="Ind.impuestos")
        ind_txt.wait_for(state="visible", timeout=1_500)
        ind_txt.fill(ind_impuestos)
        logger.info("Indicador de impuestos ingresado: %s", ind_impuestos)


    # ------------------------------------------------------------------
    # CONTABILIZACIÓN
    # ------------------------------------------------------------------

    def _ejecutar(self):
        """
        Hace clic en 'Contabilizar Resaltado' para registrar la compensación
        """
        logger.info("Ejecutando compensación")
        self.page.wait_for_load_state("networkidle")
        ejecutar = self.page.get_by_role("button", name="Contabilizar  Resaltado")
        self.page.wait_for_timeout(500)
        ejecutar.click(timeout=10_000)
        self.page.wait_for_load_state("networkidle")



    # ------------------------------------------------------------------
    # FLUJO PRINCIPAL DE COMPENSACIÓN DE CONTRAPARTIDAS
    # ------------------------------------------------------------------

    def compensar(self, conn,cuenta_ajuste_peso:str,texto_ajuste_peso:str,fecha:datetime.datetime) -> pd.DataFrame:
        """
        Itera sobre df_compensar y compensa cada par (partida + contrapartida).

        Si existe diferencia de importe entre ambos documentos, rellena el
        formulario 'Eliminar diferencias' con el ajuste correspondiente antes
        de contabilizar. La clase de documento del ajuste se determina por cuál
        de los dos documentos tiene el importe mayor.

        Args:
            conn: Instancia de SAPConexion para reiniciar la página entre filas.

        Returns:
            DataFrame de partidas actualizado (documentos compensados eliminados).
        """
        logger.info("Iniciando proceso de compensación en SAP para contrapartidas de COP")

        for idx, row in self.df_compensar.iterrows():

            # ── Reinicio de página entre iteraciones ─────────────────────
            try:
                logger.info("Reiniciando página SAP para fila %d", idx)
                self.page = conn.restart_page() if idx > 0 else self.page
            except Exception:
                logger.exception("Error al reiniciar la página SAP para fila %d", idx)
                error = pd.DataFrame([{
                    "Error en Transacción": "f-03",
                    "Descripción": f"Error al reiniciar la página SAP para compensar por contrapartida para documentos: {[row["Nº documento"], row["Contrapartida"]]}"
                    }])
                self.df_errores_sap = pd.concat([self.df_errores_sap,error], ignore_index=True)
                continue

            documentos = [row["Nº documento"], row["Contrapartida"]]

            # ── Procesamiento de la fila ──────────────────────────────────
            try:
                logger.info("Procesando fila %d | documentos=%s", idx, documentos)

                cuenta_mayor    = row["Cuenta de mayor"]
                sociedad        = row["Sociedad"]
                moneda          = row["Moneda del documento"]
                clase_documento = row["Clave contabiliz."]

                # Valores absolutos para poder comparar magnitudes sin importar signo
                if moneda == "USD":
                    valor_partida       = abs(round(float(row["Valor USD"]), 2))
                    valor_contrapartida = abs(round(float(row["valor contrapartida"]), 2))
                else:
                    valor_partida       = abs(round(int(row["Importe en moneda local"]), 0))
                    valor_contrapartida = abs(round(int(row["valor contrapartida"]), 0))

                # Determinar si existe diferencia y calcular su magnitud
                diff       = valor_partida != valor_contrapartida
                diff_value = abs(abs(valor_partida) - abs(valor_contrapartida))

                if diff:
                    logger.info(
                        "Diferencia detectada para fila %d | valor_partida=%s | valor_contrapartida=%s",
                        idx, valor_partida, valor_contrapartida
                    )
                    # La clase de documento del ajuste corresponde al documento
                    # con mayor importe; si es la contrapartida, se sobreescribe
                    if valor_contrapartida == max(valor_partida, valor_contrapartida):
                        clase_documento = row["clave contabiliz. contrapartida"]

                self._navigate_to_f03()
                self._fill_fields(sociedad, cuenta_mayor, moneda, fecha)
                self._llenar_documentos(documentos)
                self._abrir_eliminar_diferencias()

                if diff:
                    self._eliminar_diferencias(
                        texto_ajuste_peso, clase_documento, cuenta_ajuste_peso, str(diff_value)
                    )

                self._ejecutar()
                self._wait_for_loading()
                doc = self._compensacion_creada()
                if doc: #
                    logger.info("Compensación creada exitosamente para fila %d", idx)
                    self.df_partidas = self._actualizar_partidas(documentos)
                    if diff:
                        proceso = f"Compensación por contrapartida en {moneda} y ajuste al peso por ${str(diff_value)}"
                    else:
                        proceso = f"Compensación por contrapartida exacta en {moneda}"
                    compensada = {
                        "Documento creado": doc,
                        "Sociedad": sociedad,
                        "Cuenta de mayor": cuenta_mayor,
                        "Proceso": proceso,
                        "Documentos asociados": ", ".join(map(str, documentos))
                    }
                    df_compensada = pd.DataFrame([compensada])  
                    self.df_procesadas_compensadas = pd.concat(
                        [self.df_procesadas_compensadas, df_compensada], 
                        ignore_index=True
                    )
  
                else:

                    raise RuntimeError(
                        "No se detectó confirmación de creación de compensación para fila %d" % idx
                    )

            except Exception as e:
                logger.exception("Error creando compensación para documentos %s", documentos)
                error = pd.DataFrame([{
                    "Error en Transacción": "f-03",
                    "Descripción": f"{e} para documentos: {documentos}"
                    }])
                self.df_errores_sap = pd.concat([self.df_errores_sap,error], ignore_index=True)
                continue

        return self.df_partidas,self.df_procesadas_compensadas


    # ------------------------------------------------------------------
    # UTILIDADES
    # ------------------------------------------------------------------

    def _actualizar_partidas(self, documentos: list) -> pd.DataFrame:
        """Elimina del DataFrame de partidas los documentos ya compensados."""
        for p in documentos:
            self.df_partidas = self.df_partidas[self.df_partidas["Nº documento"] != p]
        return self.df_partidas
    
    def get_errors(self):
        return self.df_errores_sap