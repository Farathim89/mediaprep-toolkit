"""Localization: tr() / ntr() look up the active language catalog.

The ENGLISH source text is the key:

    from .i18n import tr, ntr, N_
    ttk.Label(f, text=tr("Output folder:"))
    status.set(tr("Found {n} files in {folder}", n=n, folder=name))
    ntr("{n} file", "{n} files", n)            # plural; key = the singular
    TITLES = (N_("Intro"), N_("Credits"))      # mark only - tr() at display

Never build the key with an f-string (tr(f"...{x}")) - the lookup would miss
and the extractor (tools/i18n_extract.py) can't see it; pass placeholders as
keyword arguments instead. Placeholders are filled with str.format AFTER the
lookup, so a translation can reorder them.

Catalogs: app/locales/<code>.json =
    {"_meta": {"name": "Svenska", "english_name": "Swedish"},
     "<english>": "<translation>",
     "<english singular>": {"one": "...", "other": "..."}}
Missing or empty entries fall back to English.

The language comes from settings.json "language" (default "en"; "auto" = automatic: the
Windows UI language when it is one of LANGUAGES, else English). It is read
lazily on the first tr() call, so module-level tr() works too; a change in the
Settings dialog takes effect after a restart.

Test hooks (environment):
    MEDIAPREP_LANG=<code>         force a language
    MEDIAPREP_LANG_FILE=<path>    load this catalog file instead (pseudo-locale)
"""
import json
import os

# code -> (native name, English name); the order is the picker order
LANGUAGES = {
    "en": ("English", "English"),
    "sv": ("Svenska", "Swedish"),
    "es": ("Español", "Spanish"),
    "de": ("Deutsch", "German"),
    "fr": ("Français", "French"),
    "pt-BR": ("Português (Brasil)", "Portuguese (Brazil)"),
    "it": ("Italiano", "Italian"),
    "ru": ("Русский", "Russian"),
    "ja": ("日本語", "Japanese"),
    "zh-CN": ("简体中文", "Chinese (Simplified)"),
    "nb": ("Norsk bokmål", "Norwegian Bokmål"),
    "da": ("Dansk", "Danish"),
    "fi": ("Suomi", "Finnish"),
    "pl": ("Polski", "Polish"),
    "nl": ("Nederlands", "Dutch"),
    "tr": ("Türkçe", "Turkish"),
    "ko": ("한국어", "Korean"),
    "hi": ("हिन्दी", "Hindi"),
    "id": ("Bahasa Indonesia", "Indonesian"),
}
DEFAULT = "en"
AUTO = "auto"                   # settings value meaning "follow Windows"

# Windows locale (language_REGION) -> catalog code, for the cases where the
# bare language part isn't enough
_LOCALE_MAP = {
    "pt": "pt-BR",              # pt_PT users get Brazilian Portuguese
    "zh_cn": "zh-CN", "zh_sg": "zh-CN",
    "no": "nb", "nn": "nb", "nb": "nb",
}


class _State:
    code = None                 # None = not initialised yet
    catalog = {}


_S = _State()


# ---------------------------------------------------------------- lookup
def tr(text, **fmt):
    """`text` in the active language (English when untranslated), with
    {name} placeholders filled from the keyword arguments."""
    if _S.code is None:
        _init()
    out = _S.catalog.get(text) if _S.catalog else None
    if isinstance(out, dict):           # a plural entry used via tr()
        out = out.get("other") or out.get("one")
    if not out or not isinstance(out, str):
        out = text
    if fmt:
        try:
            return out.format(**fmt)
        except (KeyError, IndexError, ValueError):
            try:                        # broken translation -> English
                return text.format(**fmt)
            except (KeyError, IndexError, ValueError):
                return text
    return out


_ = tr


def ntr(singular, plural, n, **fmt):
    """Plural-aware tr(): key = `singular`. The catalog value is either a
    string (used for every n) or {"one": ..., "other": ...}. `n` is also
    available as the {n} placeholder."""
    if _S.code is None:
        _init()
    fmt.setdefault("n", n)
    val = _S.catalog.get(singular) if _S.catalog else None
    out = None
    if isinstance(val, dict):
        out = val.get("one") if n == 1 else val.get("other")
        out = out or val.get("other") or val.get("one")
    elif isinstance(val, str) and val:
        out = val
    if not out:
        out = singular if n == 1 else plural
    try:
        return out.format(**fmt)
    except (KeyError, IndexError, ValueError):
        try:
            return (singular if n == 1 else plural).format(**fmt)
        except (KeyError, IndexError, ValueError):
            return singular if n == 1 else plural


def N_(text):
    """Mark `text` for extraction without translating it now (module-level
    constants that are translated with tr() where they are shown)."""
    return text


# ---------------------------------------------------------------- languages
def _locales_dir():
    from .config import resource_path
    return resource_path("locales")


def available_languages():
    """[(code, native name)] of the languages that have a catalog file
    (English always), in LANGUAGES order."""
    d = _locales_dir()
    out = []
    for code, (native, _eng) in LANGUAGES.items():
        if code == DEFAULT or os.path.isfile(os.path.join(d, code + ".json")):
            out.append((code, _meta_name(code) or native))
    return out


def _meta_name(code):
    try:
        with open(os.path.join(_locales_dir(), code + ".json"), "r", encoding="utf-8") as f:
            meta = json.load(f).get("_meta") or {}
        return meta.get("name")
    except (OSError, ValueError, AttributeError):
        return None


def native_name(code):
    return LANGUAGES.get(code, (code, code))[0]


def windows_language():
    """The catalog code matching the Windows UI language, or None."""
    try:
        import ctypes
        import locale
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid, "")
    except Exception:
        return None
    return match_locale(name)


def match_locale(name):
    """'sv_SE' / 'pt_BR' / 'zh-CN' / 'nb_NO' ... -> one of LANGUAGES, or None."""
    if not name:
        return None
    n = name.replace("-", "_").split(".")[0].lower()
    lang = n.split("_")[0]
    if n in _LOCALE_MAP:
        return _LOCALE_MAP[n]
    if lang == "zh":
        return None                     # Traditional Chinese: no catalog
    if lang in _LOCALE_MAP:
        return _LOCALE_MAP[lang]
    for code in LANGUAGES:
        if code.lower() == lang:
            return code
    return None


def resolve(setting):
    """A settings value ("" / "auto" / code) -> the language code to use."""
    s = (setting or "").strip()
    if s.lower() == AUTO:              # explicitly chosen "Automatic"
        return windows_language() or DEFAULT
    for code in LANGUAGES:
        if code.lower() == s.lower():
            return code
    return DEFAULT                     # unset / unknown -> English


def current_language():
    if _S.code is None:
        _init()
    return _S.code


def _load_catalog(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not k.startswith("_") and isinstance(v, (str, dict)) and v}


def set_language(code):
    """Activate `code` ("" = automatic). Returns the code in use. Widgets
    already built keep their text - the app restarts to switch."""
    test_file = os.environ.get("MEDIAPREP_LANG_FILE", "").strip()
    forced = os.environ.get("MEDIAPREP_LANG", "").strip()
    if test_file and os.path.isfile(test_file):
        _S.code = "test"
        _S.catalog = _load_catalog(test_file)
        return _S.code
    code = resolve(forced or code)
    _S.code = code
    _S.catalog = ({} if code == DEFAULT
                  else _load_catalog(os.path.join(_locales_dir(), code + ".json")))
    return code


def _init():
    """First use: take the language from the saved settings."""
    setting = DEFAULT
    try:
        from .config import SETTINGS_FILE
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            setting = (json.load(f) or {}).get("language", DEFAULT)
    except Exception:
        pass
    set_language(setting if isinstance(setting, str) else DEFAULT)
