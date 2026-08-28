import re
import unicodedata
from pathlib import Path
from typing import Callable

import pandas as pd

from core.logger import get_logger
from loaders.online_refunds_loader import DevolucionesOnline
from loaders.fagll03_loader import Fagll03
from loaders.procesadas import Procesadas
from matchers.fagll03_online_refund_matcher import Fagll03DevolucionesMatcher

logger = get_logger(__name__)

# Se invoca una vez por cada archivo de devoluciones pendiente y debe
# devolver la ruta del reporte FAGLL03 recién generado para esa iteración.
GeneradorFagll03 = Callable[[], Path]


class FAGLL03DevolucionesOnlineProcessor:
    """Orquestador: por cada archivo de devoluciones pendiente en la
    carpeta, genera un FAGLL03 fresco, hace el match y marca el archivo
    como procesado antes de continuar con el siguiente.

    No conoce el detalle de cómo se genera el FAGLL03 (SAP, mock, etc.):
    recibe ``generar_fagll03`` como dependencia inyectada.
    """

    COLUMNA_PROCESADAS = "Nombre archivo"

    def __init__(
        self,
        output_dir: Path,
        generar_fagll03: GeneradorFagll03,
        procesadas_path: Path | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.generar_fagll03 = generar_fagll03
        self.procesadas_path = procesadas_path or (
            self.output_dir / "procesadas.xlsx"
        )
        self.matcher = Fagll03DevolucionesMatcher()

    def procesar_archivos(self) -> pd.DataFrame:
        ruta_procesadas = self._obtener_ruta_procesadas()
        df_procesadas = Procesadas(ruta_procesadas).cargar()
        nombres_procesados = self._nombres_procesados(df_procesadas)

        resultados = []
        for archivo in self._archivos_pendientes(nombres_procesados, ruta_procesadas):
            resultado = self._procesar_un_archivo(archivo)
            resultados.append(resultado)

            df_procesadas = self._marcar_procesado(
                df_procesadas, ruta_procesadas, archivo.name
            )

        if not resultados:
            logger.info("No hay archivos de devoluciones pendientes.")
            return pd.DataFrame()
        return pd.concat(resultados, ignore_index=True)

    def _procesar_un_archivo(self, archivo: Path) -> pd.DataFrame:
        """Flujo completo para un único archivo de devoluciones."""
        logger.info("Generando FAGLL03 para: %s", archivo.name)
        archivo_fagll03 = self.generar_fagll03()
        df_fagll03 = Fagll03(archivo_fagll03).cargar()

        logger.info("Cargando devolución online: %s", archivo.name)
        df_devoluciones = DevolucionesOnline(archivo).cargar()

        logger.info("Cruzando: %s", archivo.name)
        resultado = self.matcher.match(df_fagll03, df_devoluciones)
        resultado["archivo_devolucion"] = archivo.name
        return resultado

    def _marcar_procesado(
        self,
        df_procesadas: pd.DataFrame,
        ruta_procesadas: Path,
        nombre: str,
    ) -> pd.DataFrame:
        """Añade el archivo a procesadas y persiste el excel de inmediato,
        para no reprocesarlo si el flujo se interrumpe a mitad de camino."""
        nueva_fila = pd.DataFrame({self.COLUMNA_PROCESADAS: [nombre]})
        df_procesadas = pd.concat([df_procesadas, nueva_fila], ignore_index=True)
        df_procesadas.to_excel(ruta_procesadas, index=False)
        logger.info("Archivo marcado como procesado: %s", nombre)
        return df_procesadas

    def _archivos_pendientes(
        self, nombres_procesados: set[str], ruta_procesadas: Path
    ) -> list[Path]:
        archivos = []
        for archivo in sorted(self.output_dir.glob("*.xlsx")):
            if "devoluciones" not in archivo.name.lower():
                continue
            if archivo.name.startswith("~$"):
                continue
            if archivo.resolve() == ruta_procesadas.resolve():
                continue
            if self._normalizar_nombre(archivo.name) in nombres_procesados:
                logger.info("Archivo ya procesado, se omite: %s", archivo.name)
                continue
            archivos.append(archivo)
        return archivos

    def _obtener_ruta_procesadas(self) -> Path:
        if self.procesadas_path.exists():
            return self.procesadas_path

        ruta_local = self.output_dir / "procesadas.xlsx"
        if ruta_local.exists():
            logger.info(
                "Usando archivo de procesadas encontrado en output_dir: %s",
                ruta_local,
            )
            return ruta_local

        return self.procesadas_path

    def _nombres_procesados(self, df_procesadas: pd.DataFrame) -> set[str]:
        if self.COLUMNA_PROCESADAS not in df_procesadas.columns:
            return set()
        return {
            self._normalizar_nombre(nombre)
            for nombre in df_procesadas[self.COLUMNA_PROCESADAS].dropna()
        }

    @staticmethod
    def _normalizar_nombre(valor) -> str:
        texto = str(valor).strip().casefold()
        texto = "".join(
            caracter
            for caracter in unicodedata.normalize("NFKD", texto)
            if not unicodedata.combining(caracter)
        )
        return re.sub(r"[^a-z0-9]+", "", texto)