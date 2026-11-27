"""Small gettext domains for the graphical Ersa clients."""

import gettext
import os


class Domain:
    _domains = {}

    def __new__(cls, name):
        if name not in cls._domains:
            cls._domains[name] = super().__new__(cls)
        return cls._domains[name]

    def __init__(self, name):
        if getattr(self, "name", None) == name:
            return
        self.name = name
        self.language = None
        self._key = None
        self._translation = gettext.NullTranslations()

    def set_language(self, language):
        """Override the process language, or use the session environment with None."""
        self.language = language
        self._key = None

    def _catalog(self):
        language = self.language
        if language is None:
            language = next((os.environ[key] for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
                             if os.environ.get(key)), "C")
        key = (language, os.environ.get("ECP_LOCALEDIR"))
        if key != self._key:
            self._key = key
            self._translation = gettext.NullTranslations()
            languages = language.split(":")
            for base in ("/usr/share/locale", os.environ.get("ECP_LOCALEDIR")):
                if not base:
                    continue
                catalog = gettext.translation(self.name, localedir=base, languages=languages, fallback=True)
                catalog.add_fallback(self._translation)
                self._translation = catalog
        return self._translation

    def __call__(self, message):
        return self._catalog().gettext(message)

    def ngettext(self, singular, plural, count):
        return self._catalog().ngettext(singular, plural, count)
