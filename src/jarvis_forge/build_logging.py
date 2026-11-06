"""Jarvis-HEP-compatible process log layout without depending on Jarvis-HEP."""

from datetime import datetime
from pathlib import Path

from .build_cards import default_card


def format_record(message, *, module="Jarvis-Forge.Build", level="INFO", timestamp=None):
    config = default_card("logging")[0]["process"]
    timestamp = timestamp or datetime.now().astimezone()
    date = timestamp.strftime(config["date_format"])
    digits = config["millis_digits"]
    if digits:
        date += "." + f"{timestamp.microsecond:06d}"[:digits]
    return config["head"].format(bullet=config["bullet"], module=module, timestamp=date,
                                 level=level, message=message) + str(message) + "\n"


class ProcessLog:
    def __init__(self, path: Path, module: str, progress=None):
        self.path, self.module, self.progress = path, module, progress

    def write(self, message, level="INFO", module=None):
        text = format_record(message, module=module or self.module, level=level)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(text)
        return text

    def __call__(self, message, level="INFO", module=None):
        text = self.write(message, level=level, module=module)
        if self.progress:
            self.progress(text.rstrip("\n"))
