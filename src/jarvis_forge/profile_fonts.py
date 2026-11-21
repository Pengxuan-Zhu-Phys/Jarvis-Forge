"""Fontconfig settings for the installed fonts in one Spack profile."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as xml

from .package_operations import _inside
from .storage import write_text


FONT_SUFFIXES = {".otf", ".otc", ".ttf", ".ttc"}


def _font_directories(store, packages):
    store = store.resolve()
    directories = set()
    for package in packages:
        if package["external"]:
            continue
        prefix = Path(package["prefix"]).resolve()
        if not prefix.is_relative_to(store):
            continue
        directory = prefix / "share/fonts"
        if not directory.is_dir():
            continue
        directory = directory.resolve()
        if not directory.is_relative_to(prefix):
            continue
        if any(font.suffix.lower() in FONT_SUFFIXES and font.is_file()
               and font.resolve().is_relative_to(directory) for font in directory.rglob("*")):
            directories.add(directory)
    return sorted(directories)


def write_config(home, store, revision, packages):
    directories = _font_directories(store, packages)
    if not directories:
        (revision/"fonts.conf").unlink(missing_ok=True)
        return None

    cache = _inside(home, home.root / "cache/fontconfig")
    cache.mkdir(parents=True, exist_ok=True)
    config = _inside(home, revision / "fonts.conf")
    document = xml.Element("fontconfig")
    for directory in directories:
        xml.SubElement(document, "dir").text = str(directory)
    # Keep the Linux system font directories visible. Use a standalone config
    # so neither site includes nor Fontconfig defaults can add HOME cache paths.
    for directory in ("/usr/local/share/fonts", "/usr/share/fonts"):
        xml.SubElement(document, "dir").text = directory
    xml.SubElement(document, "cachedir").text = str(cache)
    xml.indent(document)
    write_text(config, '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
               + xml.tostring(document, encoding="unicode") + "\n")
    return config
