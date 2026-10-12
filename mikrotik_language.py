"""Select Polish or English messages using the user's operating system language."""

import argparse
import ctypes
import locale
import os
import re
import subprocess
import sys


def _is_polish(language: str) -> bool:
    return re.split(r"[-_.@]", language.strip().lower(), maxsplit=1)[0] == "pl"


def detect_language() -> str:
    """Return 'pl' only when Polish is detected; otherwise use English."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode == 0:
                # defaults prints an array such as ("pl-PL", "en-US").
                languages = re.findall(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*", result.stdout)
                if languages:
                    return "pl" if _is_polish(languages[0]) else "en"
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "win32":
        try:
            language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if language_id:
                return "pl" if language_id & 0x3ff == 0x15 else "en"
        except (AttributeError, OSError):
            pass

    # On Linux, LANGUAGE can contain an ordered list, e.g. pl:en.
    for key in ("LC_ALL", "LANGUAGE", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key, "").strip()
        if value:
            return "pl" if _is_polish(value.split(":", 1)[0]) else "en"
    try:
        language = locale.getlocale()[0]
    except (ValueError, TypeError, locale.Error):
        language = None
    return "pl" if language and _is_polish(language) else "en"


LANGUAGE = detect_language()


def tr(polish: str, english: str) -> str:
    return polish if LANGUAGE == "pl" else english


def add_language_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lang", choices=("pl", "en"),
        help=tr("język komunikatów (domyślnie wykrywany z systemu)",
                "message language (default: detected from the system)"),
    )


def configure_language() -> None:
    """Apply --lang before building localized help and parsing other options."""
    global LANGUAGE
    parser = argparse.ArgumentParser(add_help=False)
    add_language_argument(parser)
    args, _ = parser.parse_known_args()
    if args.lang is not None:
        LANGUAGE = args.lang
