from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .models import CalibrationState


class PersistedSettings(BaseModel):
    schema_version: str = "1.0"
    calibration: CalibrationState = Field(default_factory=CalibrationState)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> PersistedSettings:
        if not self.path.exists():
            return PersistedSettings()
        try:
            return PersistedSettings.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return PersistedSettings()

    def save_calibration(self, calibration: CalibrationState) -> None:
        settings = PersistedSettings(calibration=calibration)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            settings.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
