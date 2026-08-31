from __future__ import annotations

import json
import sys
from pathlib import Path


def application_dir() -> Path:
    """Carpeta del ejecutable con PyInstaller o raíz del proyecto en desarrollo."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    return application_dir() / "config.json"


def load_config() -> dict[str, str]:
    path = config_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def save_config(values: dict[str, str]) -> None:
    """Guarda la configuración elegida por el usuario junto a la aplicación."""
    with config_path().open("w", encoding="utf-8") as file:
        json.dump(values, file, ensure_ascii=False, indent=2)


def get_included_bank_payments() -> list[str]:
    return ["PAGO EXITOSO Y ABONADO"]