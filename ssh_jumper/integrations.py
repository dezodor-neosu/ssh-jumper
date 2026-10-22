"""User-selected external text editor and persistent application settings."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import subprocess


@dataclass
class IntegrationSettings:
    editor_path: str = ""
    editor_arguments: list[str] = field(default_factory=list)


def detect_settings():
    return IntegrationSettings()


def executable(value):
    path = Path(os.path.expandvars(value)).expanduser()
    if not value or not path.is_file() or path.suffix.lower() != ".exe":
        raise ValueError("Choose an existing editor .exe file in App settings.")
    return str(path.resolve())


def validate_settings(settings):
    if settings.editor_path:
        executable(settings.editor_path)
    if not isinstance(settings.editor_arguments, list) or any(
            not isinstance(arg, str) or "\x00" in arg for arg in settings.editor_arguments):
        raise ValueError("Editor arguments must be a JSON list of strings.")


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return IntegrationSettings()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") == 1:
            # Discard obsolete SSH client preferences during migration.
            return IntegrationSettings()
        if data.get("version") != 2:
            raise ValueError("Unsupported app settings version")
        return IntegrationSettings(**data["editor"])

    def save(self, settings):
        validate_settings(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "editor": asdict(settings)}, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def launch_editor(settings, file_path):
    validate_settings(settings)
    path = executable(settings.editor_path)
    return subprocess.Popen([path, *settings.editor_arguments, str(Path(file_path).resolve())],
                            cwd=str(Path(path).parent), shell=False)
