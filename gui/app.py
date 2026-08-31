from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.configuration import load_config, save_config

BLUE, BLUE_HOVER = "#1976B6", "#125A8C"
BACKGROUND, SURFACE, TEXT, MUTED = "#0F1720", "#17222E", "#F4F8FC", "#B7C5D3"


class _QueueHandler(logging.Handler):
    def __init__(self, events: queue.Queue[logging.LogRecord]):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(record)


class CompensationApp(ctk.CTk):
    """Pantalla de usuario; SAP corre en worker y la UI sólo consume colas."""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Compensar devoluciones Online")
        self.geometry("1180x720")
        self.minsize(980, 620)
        self.configure(fg_color=BACKGROUND)
        self.events: queue.Queue[logging.LogRecord] = queue.Queue()
        self.worker_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self.values = load_config()
        self.summary_path: Path | None = None
        self.file_inputs: dict[str, ctk.CTkEntry] = {}
        self._build()
        logging.getLogger().addHandler(_QueueHandler(self.events))
        self.after(100, self._drain_queues)

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Compensar devoluciones Online", font=("Segoe UI", 25, "bold"), text_color=TEXT).grid(row=0, column=0, padx=28, pady=(18, 0), sticky="w")
        ctk.CTkLabel(header, text="GCO - Tesorería", font=("Segoe UI", 14), text_color=MUTED).grid(row=1, column=0, padx=28, pady=(0, 18), sticky="w")
        ctk.CTkButton(header, text="Configuración SAP", fg_color=BLUE, hover_color=BLUE_HOVER, command=self._open_sap_settings).grid(row=0, column=1, rowspan=2, padx=24, pady=18)

        left = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=12)
        left.grid(row=1, column=0, padx=(24, 12), pady=24, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(left, text="Archivos de ejecución", font=("Segoe UI", 18, "bold"), text_color=TEXT).grid(row=0, column=0, padx=20, pady=(20, 12), sticky="w")
        for row, (name, label) in enumerate((("output_dir", "Carpeta de salida"), ("online_refunds", "Devoluciones Online"), ("bank_voucher", "Comprobante banco")), start=1):
            ctk.CTkLabel(left, text=label, text_color=MUTED).grid(row=row * 2 - 1, column=0, padx=20, pady=(10, 3), sticky="w")
            field = ctk.CTkFrame(left, fg_color="transparent")
            field.grid(row=row * 2, column=0, padx=20, sticky="ew")
            field.grid_columnconfigure(0, weight=1)
            entry = ctk.CTkEntry(field, height=36, fg_color=BACKGROUND, border_color="#30465A", text_color=TEXT)
            entry.insert(0, self.values.get(name, ""))
            entry.grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(field, text="Buscar", width=75, fg_color="#2B4053", hover_color="#3A5770", command=lambda key=name: self._browse(key)).grid(row=0, column=1, padx=(8, 0))
            self.file_inputs[name] = entry
        self.run_button = ctk.CTkButton(left, text="EJECUTAR PROCESO", height=42, fg_color=BLUE, hover_color=BLUE_HOVER, font=("Segoe UI", 14, "bold"), command=self._start)
        self.run_button.grid(row=8, column=0, padx=20, pady=(28, 10), sticky="ew")
        self.summary_button = ctk.CTkButton(left, text="ABRIR RESUMEN", height=38, fg_color="transparent", border_width=1, border_color=BLUE, text_color=TEXT, hover_color="#203B52", state="disabled", command=self._open_summary)
        self.summary_button.grid(row=9, column=0, padx=20, pady=(0, 20), sticky="ew")

        right = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=12)
        right.grid(row=1, column=1, padx=(12, 24), pady=24, sticky="nsew")
        right.grid_columnconfigure(0, weight=1); right.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(right, text="Actividad del proceso", font=("Segoe UI", 18, "bold"), text_color=TEXT).grid(row=0, column=0, padx=20, pady=(20, 5), sticky="w")
        self.status = ctk.CTkLabel(right, text="Listo para iniciar.", anchor="w", text_color=MUTED, fg_color="#20303E", corner_radius=8, height=38)
        self.status.grid(row=1, column=0, padx=20, pady=(6, 12), sticky="ew")
        self.console = ctk.CTkTextbox(right, state="disabled", wrap="word", fg_color=BACKGROUND, text_color=TEXT, font=("Cascadia Mono", 12))
        self.console.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="nsew")
        for tag, color in {"INFO": TEXT, "WARNING": "#F3B63A", "ERROR": "#FF6B6B", "CRITICAL": "#FF4D4D"}.items():
            self.console._textbox.tag_config(tag, foreground=color)

    def _browse(self, name: str) -> None:
        path = filedialog.askdirectory() if name == "output_dir" else filedialog.askopenfilename()
        if path:
            self.file_inputs[name].delete(0, "end"); self.file_inputs[name].insert(0, path)

    def _open_sap_settings(self) -> None:
        dialog = ctk.CTkToplevel(self); dialog.title("Configuración SAP"); dialog.geometry("540x440"); dialog.transient(self); dialog.grab_set(); dialog.configure(fg_color=BACKGROUND)
        entries: dict[str, ctk.CTkEntry] = {}
        for row, (key, label) in enumerate((("sap_url", "URL SAP"), ("sap_user", "Usuario SAP"), ("sap_password", "Contraseña SAP"), ("layout", "Layout SAP"))):
            ctk.CTkLabel(dialog, text=label, text_color=MUTED).grid(row=row * 2, column=0, padx=28, pady=(18, 3), sticky="w")
            entry = ctk.CTkEntry(dialog, width=480, height=36, show="*" if key == "sap_password" else None, fg_color=SURFACE, text_color=TEXT)
            entry.insert(0, self.values.get(key, "")); entry.grid(row=row * 2 + 1, column=0, padx=28, sticky="ew"); entries[key] = entry
        def save() -> None:
            self.values.update({key: entry.get().strip() for key, entry in entries.items()})
            self._save_current_config()
            dialog.destroy()
            self._set_status("Configuración guardada en config.json.", MUTED)
        ctk.CTkButton(dialog, text="GUARDAR", fg_color=BLUE, hover_color=BLUE_HOVER, command=save).grid(row=8, column=0, padx=28, pady=26, sticky="ew")

    def _start(self) -> None:
        values = {**self.values, **{key: entry.get().strip() for key, entry in self.file_inputs.items()}}
        required = ("output_dir", "online_refunds", "bank_voucher", "layout", "sap_user", "sap_password", "sap_url")
        invalid_files = not Path(values["output_dir"]).is_dir() or not Path(values["online_refunds"]).is_file() or not Path(values["bank_voucher"]).is_file()
        if any(not values.get(key) for key in required) or invalid_files:
            messagebox.showerror("Datos incompletos", "Seleccione archivos y carpeta válidos, y complete Configuración SAP."); return
        self.values.update(values)
        self._save_current_config()
        self.summary_path = None; self.summary_button.configure(state="disabled"); self.run_button.configure(state="disabled")
        self.console.configure(state="normal"); self.console.delete("1.0", "end"); self.console.configure(state="disabled")
        self._set_status("Proceso en ejecución: SAP está trabajando…", "#7FC8FF")
        threading.Thread(target=self._run_worker, args=(values,), daemon=True).start()

    def _run_worker(self, values: dict[str, str]) -> None:
        try:
            from services.sap_orchestrator import run
            import pandas as pd
            args = argparse.Namespace(**values, company_code="1000", customer_account="20001\n1000004288", report_date=pd.Timestamp.now(), report_name="fbl5n_report_TEMP")
            self.worker_results.put(("finished", run(args)))
        except Exception as error:
            logging.getLogger(__name__).exception("Error no controlado: %s", error); self.worker_results.put(("error", error))

    def _drain_queues(self) -> None:
        while not self.events.empty():
            record = self.events.get_nowait(); timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            self.console.configure(state="normal"); self.console.insert("end", f"[{timestamp}] {record.levelname:<8} {record.getMessage()}\n", record.levelname); self.console.see("end"); self.console.configure(state="disabled")
        while not self.worker_results.empty():
            kind, result = self.worker_results.get_nowait(); self.run_button.configure(state="normal")
            if kind == "finished": self._finish(result)
            else: self._set_status("El proceso finalizó con un error inesperado.", "#FF6B6B")
        self.after(100, self._drain_queues)

    def _finish(self, summary: object) -> None:
        failed = getattr(summary, "failed", 1); payment_date = getattr(summary, "payment_date")
        self.summary_path = Path(self.file_inputs["output_dir"].get()) / f"compensacion_dev_online_{payment_date:%d-%m-%Y}" / "resumen.txt"
        self.summary_button.configure(state="normal" if self.summary_path.exists() else "disabled")
        if failed: self._set_status(f"Proceso finalizado con {failed} detalle(s). Revise el resumen.", "#F3B63A")
        else: self._set_status("Proceso finalizado correctamente: la compensación F-03 fue confirmada.", "#73D59A")

    def _open_summary(self) -> None:
        if not self.summary_path or not self.summary_path.exists(): messagebox.showwarning("Resumen no disponible", "Aún no se ha generado el resumen de esta ejecución."); return
        if sys.platform.startswith("win"): os.startfile(self.summary_path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin": subprocess.run(["open", str(self.summary_path)], check=False)
        else: subprocess.run(["xdg-open", str(self.summary_path)], check=False)

    def _set_status(self, message: str, color: str) -> None:
        self.status.configure(text=message, text_color=color)

    def _save_current_config(self) -> None:
        """Persiste SAP y las rutas principales tal como están en la pantalla."""
        self.values.update({key: entry.get().strip() for key, entry in self.file_inputs.items()})
        save_config(self.values)


def main() -> None:
    CompensationApp().mainloop()


if __name__ == "__main__": main()
