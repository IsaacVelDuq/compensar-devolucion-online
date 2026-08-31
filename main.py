"""Punto de entrada de la aplicación de escritorio.

La automatización SAP vive en ``services.sap_orchestrator`` para que la GUI
pueda ejecutarla en un worker sin crear una segunda ventana.
"""

from gui.app import CompensationApp


def main() -> None:
    app = CompensationApp()
    app.mainloop()


if __name__ == "__main__":
    main()
