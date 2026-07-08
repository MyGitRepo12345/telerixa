import json
from pathlib import Path


DEFAULT_LANGUAGE = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_catalog_cache = {}
_current_language = DEFAULT_LANGUAGE


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _locale_path(language):
    return LOCALES_DIR / f"{language}.json"


def normalize_language(language):
    value = str(language or "").strip().lower().replace("_", "-")
    if not value:
        return DEFAULT_LANGUAGE

    candidates = [value]
    if "-" in value:
        candidates.append(value.split("-", 1)[0])

    for candidate in candidates:
        if _locale_path(candidate).is_file():
            return candidate

    return DEFAULT_LANGUAGE


def load_catalog(language):
    language = normalize_language(language)
    if language in _catalog_cache:
        return _catalog_cache[language]

    try:
        with _locale_path(language).open("r", encoding="utf-8") as file:
            catalog = json.load(file)
    except (OSError, json.JSONDecodeError):
        catalog = {}

    if not isinstance(catalog, dict):
        catalog = {}

    _catalog_cache[language] = catalog
    return catalog


def configure_language(language):
    global _current_language
    _current_language = normalize_language(language)
    load_catalog(DEFAULT_LANGUAGE)
    load_catalog(_current_language)
    return _current_language


def get_language():
    return _current_language


def get_language_options():
    options = {}
    if not LOCALES_DIR.is_dir():
        return {DEFAULT_LANGUAGE: "English"}

    for path in sorted(LOCALES_DIR.glob("*.json")):
        language = normalize_language(path.stem)
        catalog = load_catalog(language)
        label = catalog.get("language.name", language)
        options[language] = label

    if DEFAULT_LANGUAGE not in options:
        options[DEFAULT_LANGUAGE] = "English"

    return options


def tr(key, **params):
    language_catalog = load_catalog(_current_language)
    default_catalog = load_catalog(DEFAULT_LANGUAGE)
    template = language_catalog.get(key, default_catalog.get(key, key))

    if not isinstance(template, str):
        template = str(template)

    if not params:
        return template

    try:
        return template.format_map(_SafeFormatDict(params))
    except Exception:
        return template
