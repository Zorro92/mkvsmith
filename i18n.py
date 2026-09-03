"""
Internationalisation (i18n) for mkvsmith.

Uses the English source string itself as the lookup key (gettext-style), so any
untranslated phrase falls back to English automatically — no separate English
catalogue is needed, and adding a new user-facing string never breaks existing
locales.

Language resolution order (first match wins):
    1. --ui-lang flag            (explicit, overrides everything)
    2. settings file ("language" key in ~/.mkvsmith_config.json)
    3. LC_MESSAGES / LANG env    (POSIX locale auto-detection)
    4. "en"                      (default)

Usage at call sites:

    log_info(tr("Goodbye!"))
    log_info(tr("Ripping {n} episode(s)...", n=len(ep_titles)))

Only user-facing strings go through tr(); log_debug stays in English on purpose.
"""

from __future__ import annotations

import os
from typing import Any

# =============================================================================
# Catalogues
# =============================================================================

# Each locale maps {english_source: translated}. Missing keys fall back to the
# English source string itself, so partial translations are safe.
_ES: dict[str, str] = {
    # --- main() startup / flow -------------------------------------------------
    "Missing: mkvmerge (install mkvtoolnix)": "Falta: mkvmerge (instala mkvtoolnix)",
    "A source path is required": "Se requiere una ruta de origen",
    "Run with -h to see usage, e.g. script.py /path/to/media": "Ejecuta con -h para ver el uso, p. ej. script.py /ruta/al/medio",
    "No titles found": "No se encontraron títulos",
    "No titles": "Sin títulos",
    "No episodes detected on this disc": "No se detectaron episodios en este disco",
    "Ripping {n} episode(s)...": "Extrayendo {n} episodio(s)...",
    "Main feature: #{idx} {name} ({dur})": "Película principal: #{idx} {name} ({dur})",
    "Main feature: #{idx} {name}": "Película principal: #{idx} {name}",
    "\nSummary: {ok} ok, {fail} failed": "\nResumen: {ok} ok, {fail} fallidos",
    "\nDone: {ok} ok, {fail} failed": "\nListo: {ok} ok, {fail} fallidos",
    # --- interactive_mode ------------------------------------------------------
    "TMDB tagging available (key found) -- you'll be asked per rip.": "Etiquetado TMDB disponible (clave encontrada) -- se te preguntará por cada extracción.",
    "[n]=details  r N=rip title N  re=rip all episodes  ra=rip all  q=quit": "[n]=detalles  r N=extraer título N  re=extraer todos los episodios  ra=extraer todo  q=salir",
    "[n]=details  r N=rip title N  rm=main feature  ra=rip all  q=quit": "[n]=detalles  r N=extraer título N  rm=película principal  ra=extraer todo  q=salir",
    "Goodbye!": "¡Adiós!",
    "Usage: r N  (e.g. 'r 1')": "Uso: r N  (p. ej. 'r 1')",
    "Num required": "Se requiere un número",
    "Invalid: {idx}": "No válido: {idx}",
    "Unknown: {cmd}": "Desconocido: {cmd}",
    # --- multi-edition ---------------------------------------------------------
    "--multi-edition expects comma-separated title numbers": "--multi-edition espera números de título separados por comas",
    "--multi-edition needs at least two titles": "--multi-edition necesita al menos dos títulos",
    "--multi-edition is experimental; pass --debug to enable it": "--multi-edition es experimental; usa --debug para habilitarlo",
    "Multi-edition titles must come from the same source disc": "Los títulos de multi-edición deben provenir del mismo disco de origen",
    "Multi-edition needs at least two titles": "Multi-edición necesita al menos dos títulos",
    "No edition groups detected; specify titles: me N N ...": "No se detectaron grupos de ediciones; especifica títulos: me N N ...",
    "Using detected edition group: {idxs}": "Usando grupo de ediciones detectado: {idxs}",
    "Name each edition (shown by players in the edition picker). Press Enter to accept the default.": "Nombra cada edición (los reproductores la muestran en el selector). Pulsa Enter para aceptar la predeterminada.",
    "Edition {n} (title {idx}) name": "Nombre de la edición {n} (título {idx})",
    "Edition {n}/{total}: {name} ({dur})": "Edición {n}/{total}: {name} ({dur})",
    "Combining {n} editions into one multi-edition MKV (requires a player with ordered-chapters support)": "Combinando {n} ediciones en un MKV multi-edición (requiere un reproductor con soporte de capítulos ordenados)",
    "Titles {idxs} look like editions of the same movie - combine them with: me {idxs}": "Los títulos {idxs} parecen ediciones de la misma película: combínalos con: me {idxs}",
    "me N N ...=multi-edition rip (no args = auto-detect)": "me N N ...=extracción multi-edición (sin argumentos = detección automática)",
    # --- display_titles --------------------------------------------------------
    "  SCANNED TITLES": "  TÍTULOS ESCANEADOS",
    "Dur": "Dur",
    "Name": "Nombre",
    "Streams": "Pistas",
    "Total: {n} title(s)": "Total: {n} título(s)",
    "({n} episode(s) detected)": "({n} episodio(s) detectado(s))",
    "({n} low-quality titles hidden; use --show-all to view)": "({n} títulos de baja calidad ocultos; usa --show-all para verlos)",
    "\u2605 = main feature": "\u2605 = película principal",
    # --- display_title_details -------------------------------------------------
    "Title {idx}: {name}": "Título {idx}: {name}",
    "Source: {name}": "Origen: {name}",
    "Source: {src}": "Origen: {src}",
    "Duration: {dur}": "Duración: {dur}",
    "Title: {name}": "Título: {name}",
    # --- RipError.format_verbose ----------------------------------------------
    "ERROR: {msg}": "ERROR: {msg}",
    "CAUSE: {cause}": "CAUSA: {cause}",
    # --- settings / first-run wizard ------------------------------------------
    "First-time setup": "Configuración inicial",
    "Select language / Seleccione el idioma:": "Seleccione el idioma:",
    "  {n}. {name}": "  {n}. {name}",
    "Choice": "Opción",
    "Would you like to add a TMDB API key now? (optional, enables tagging)": "¿Deseas añadir una clave de API de TMDB ahora? (opcional, habilita el etiquetado)",
    "Enter TMDB API key (or press Enter to skip):": "Introduce la clave de API de TMDB (o pulsa Enter para omitir):",
    "Setup complete. Settings saved to {path}": "Configuración completa. Ajustes guardados en {path}",
    "Using language: {name} ({code})": "Usando idioma: {name} ({code})",
    # --- tagger prompts --------------------------------------------------------
    "Look up & tag this rip on TMDB?": "¿Buscar y etiquetar esta extracción en TMDB?",
    "Attach artwork?": "¿Adjuntar arte?",
    "  1. None": "  1. Ninguno",
    "  2. Poster": "  2. Póster",
    "  3. Backdrop": "  3. Imagen de fondo",
    "  4. Both": "  4. Ambos",
    "Choose": "Elegir",
    "Metadata Preview": "Vista previa de metadatos",
    "Could not write config: {err}": "No se pudo escribir la configuración: {err}",
    "Could not write tag XML: {err}": "No se pudo escribir el XML de etiquetas: {err}",
    "Tagging failed (ripping without tags): {err}": "El etiquetado falló (extrayendo sin etiquetas): {err}",
    # --- muxing ----------------------------------------------------------------
    "Muxing: {name}...": "Multiplexando: {name}...",
    "Created: {name} ({size:.1f} {unit})": "Creado: {name} ({size:.1f} {unit})",
    # --- scan status -----------------------------------------------------------
    "Source type: {type}": "Tipo de origen: {type}",
    "Using ISO file in directory: {name}": "Usando archivo ISO del directorio: {name}",
    "No ISO file found in {path}": "No se encontró ningún archivo ISO en {path}",
    "{path} is not a valid ISO image (missing ISO9660 PVD)": "{path} no es una imagen ISO válida (falta la PVD ISO9660)",
    "Scanning ISO with 7z...": "Escaneando ISO con 7z...",
    "7z could not find any .mpls, .m2ts, or .vob files inside the ISO.": "7z no pudo encontrar ningún archivo .mpls, .m2ts o .vob dentro de la ISO.",
    "Disc name from bdmt.xml: {name}": "Nombre del disco desde bdmt.xml: {name}",
    "Disc name: {name}": "Nombre del disco: {name}",
    "VMG disc name: {name}": "Nombre del disco VMG: {name}",
    "Detected DVD VIDEO_TS structure in ISO": "Estructura DVD VIDEO_TS detectada en la ISO",
    "Detected {n} episode(s) in VTS {vts}": "Se detectaron {n} episodio(s) en el VTS {vts}",
    " (play-all PGC {pgc})": " (PGC reproducir-todos {pgc})",
    "mkvmerge not available, cannot scan video file.": "mkvmerge no disponible, no se puede escanear el archivo de vídeo.",
    "Device read failed (needs libdvdcss/libaacs)": "Error de lectura del dispositivo (necesita libdvdcss/libaacs)",
    "Mounted {path} but found neither BDMV nor VIDEO_TS at the top level.": "Se montó {path} pero no se encontró ni BDMV ni VIDEO_TS en el nivel superior.",
    # --- disc_reader (mount / 7z) ---------------------------------------------
    "Skipping direct mount (--no-sudo is set)": "Omitiendo montaje directo (--no-sudo está activado)",
    "[INFO] Attempt to mount '{path}' via 'sudo mount -o loop,ro'? [y/N]:": "[INFO] ¿Intentar montar '{path}' con 'sudo mount -o loop,ro'? [s/N]:",
    "Attempting direct mount via 'sudo mount -o loop,ro'...": "Intentando montaje directo con 'sudo mount -o loop,ro'...",
    "mount failed (rc={rc}): {err}": "el montaje falló (rc={rc}): {err}",
    "mount/sudo not found on PATH.": "mount/sudo no encontrado en el PATH.",
    "mount exception: {err}": "excepción de montaje: {err}",
    "Temp dir '{dir}' is RAM-backed; limiting extracts to {gb:.1f} GB "
    "({pct:.0%} of {total_gb:.1f} GB RAM). Oversized titles spill to disk.": "El directorio temporal '{dir}' está en RAM; limitando las extracciones "
    "a {gb:.1f} GB ({pct:.0%} de {total_gb:.1f} GB de RAM). Los títulos "
    "demasiado grandes se pasan al disco.",
    "Temp dir '{dir}' is RAM-backed but installed RAM could not be "
    "detected; large rips may exhaust memory. Use --temp-dir to point "
    "at a disk-backed path.": "El directorio temporal '{dir}' está en RAM, pero no se pudo detectar la "
    "RAM instalada; las extracciones grandes pueden agotar la memoria. Usa "
    "--temp-dir para apuntar a una ruta en disco.",
    "Title estimated at {est:.1f} GB exceeds the RAM budget of {budget:.1f} GB; "
    "using disk-backed temp '{dir}' for this title.": "El título, estimado en {est:.1f} GB, supera el presupuesto de RAM de "
    "{budget:.1f} GB; usando el directorio temporal en disco '{dir}' para "
    "este título.",
    "Title estimated at {est:.1f} GB fits the RAM budget of {budget:.1f} GB "
    "but available memory is low ({avail:.1f} GB free); "
    "using disk-backed temp '{dir}' for this title.": "El título, estimado en {est:.1f} GB, cabe en el presupuesto de RAM de "
    "{budget:.1f} GB pero la memoria disponible es baja ({avail:.1f} GB libres); "
    "usando el directorio temporal en disco '{dir}' para este título.",
    "7z missing. Install with: sudo apt install p7zip-full": "Falta 7z. Instálalo con: sudo apt install p7zip-full",
    "7z failed: {err}": "7z falló: {err}",
    "7z extraction failed: {err}": "la extracción con 7z falló: {err}",
    "7z exception: {err}": "excepción de 7z: {err}",
    # --- misc ------------------------------------------------------------------
    "Not found: {path}": "No encontrado: {path}",
    # --- argparse / --help -----------------------------------------------------
    "MakeMKV-like ripper using mkvmerge (MKVToolNix) + 7z": "Extractor tipo MakeMKV usando mkvmerge (MKVToolNix) + 7z",
    "output directory (default: current directory)": "directorio de salida (predeterminado: directorio actual)",
    "rip only the detected main feature": "extraer solo la película principal detectada",
    "rip all detected TV-series episodes": "extraer todos los episodios de series detectados",
    "rip the given playlist titles as ONE multi-edition MKV (comma-separated title numbers, first is the default edition); e.g. --multi-edition 0,1,2": "extrae los títulos de playlist indicados como UN MKV multi-edición (números de título separados por comas, el primero es la edición predeterminada); p. ej. --multi-edition 0,1,2",
    "show all titles including low-quality ones (menus, trailers, etc.)": "mostrar todos los títulos, incluidos los de baja calidad (menús, tráilers, etc.)",
    "directory for temporary files (default: system temp dir, often /tmp/tmpfs). ": "directorio para archivos temporales (predeterminado: tmp del sistema, a menudo /tmp/tmpfs). ",
    "Set to a disk-backed path when ripping large ISOs to avoid filling RAM.": "Usa una ruta en disco al extraer ISOs grandes para evitar llenar la RAM.",
    "max fraction of installed RAM that RAM-backed (tmpfs) temp dirs may "
    "use before spilling to disk (default: 0.8). 0 disables the check.": "fracción máxima de la RAM instalada que los directorios temporales en "
    "RAM (tmpfs) pueden usar antes de pasar al disco (predeterminado: 0.8). "
    "0 desactiva la comprobación.",
    "skip all sudo-based ISO mounting (loop mount, etc.)": "omitir todo el montaje de ISO con sudo (loop mount, etc.)",
    "do not tag, even in interactive mode when a TMDB key is available": "no etiquetar, ni siquiera en modo interactivo cuando hay una clave de TMDB",
    "fetch TMDB metadata and tag each rip during muxing": "obtener metadatos de TMDB y etiquetar cada extracción durante el muxado",
    "TMDB API key (or set TMDB_API_KEY, or store with --save-key)": "clave de API de TMDB (o define TMDB_API_KEY, o guárdala con --save-key)",
    "store the TMDB API key to the config file and exit": "guardar la clave de API de TMDB en el archivo de configuración y salir",
    "metadata properties to fetch (default: a sensible set)": "propiedades de metadatos a obtener (predeterminado: un conjunto razonable)",
    "ISO 3166-1 region for content rating (default: US)": "región ISO 3166-1 para la clasificación de contenido (predeterminado: US)",
    "TMDB language code for localized metadata (e.g. en, ja, fr)": "código de idioma de TMDB para metadatos localizados (p. ej. en, ja, fr)",
    "download and embed cover art from TMDB into the MKV": "descargar e incrustar arte de portada de TMDB en el MKV",
    "keep the XML tag file after muxing": "conservar el archivo XML de etiquetas después del muxado",
    "skip the per-rip tagging confirmation prompt": "omitir el mensaje de confirmación de etiquetado por extracción",
    "override the movie title used for the TMDB search": "sobrescribir el título de la película usado en la búsqueda de TMDB",
    "override the release year used for the TMDB search": "sobrescribir el año de estreno usado en la búsqueda de TMDB",
    "UI language code (e.g. en, es); overrides the settings file": "código de idioma de la interfaz (p. ej. en, es); ignora el archivo de ajustes",
}

# Locale code -> catalogue. Add a language by dropping a dict here.
_TRANSLATIONS: dict[str, dict[str, str]] = {
    "es": _ES,
}

# Locale code -> human-readable name (shown in the language picker).
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Español",
}

# =============================================================================
# State
# =============================================================================

_ACTIVE_LANG = "en"
_ACTIVE: dict[str, str] = {}


# =============================================================================
# API
# =============================================================================


def set_language(lang: str | None) -> str:
    """Activate *lang* (e.g. "es", "en"). Falls back to "en".

    Accepts full locale strings like "es_ES.UTF-8" (the part before "." and "_"
    is taken as the language code). Returns the resolved code actually applied.
    """
    global _ACTIVE_LANG, _ACTIVE
    code = _normalise(lang)
    if code not in _TRANSLATIONS and code != "en":
        # Unknown / unsupported language -> English.
        code = "en"
    _ACTIVE_LANG = code
    _ACTIVE = _TRANSLATIONS.get(code, {})
    return code


def get_language() -> str:
    return _ACTIVE_LANG


def available_languages() -> list[tuple[str, str]]:
    """Return [(code, name), ...] for the picker, English always first."""
    out = [("en", _LANGUAGE_NAMES.get("en", "English"))]
    for code in _TRANSLATIONS:
        if code != "en":
            out.append((code, _LANGUAGE_NAMES.get(code, code)))
    return out


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(_normalise(code), code)


def tr(text: str, **kwargs: Any) -> str:
    """Translate *text* to the active locale, then format with *kwargs*.

    The English source string is the key; an unknown key returns the source
    unchanged, so untranslated phrases simply render in English.
    """
    out = _ACTIVE.get(text, text)
    if kwargs:
        try:
            out = out.format(**kwargs)
        except (KeyError, IndexError):
            # A mismatched placeholder should never break output; fall back.
            pass
    return out


def detect_locale_language() -> str | None:
    """Best-effort POSIX locale detection from LC_MESSAGES / LANG."""
    for var in ("LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val and val.upper() not in ("", "C", "POSIX"):
            code = _normalise(val)
            if code in _TRANSLATIONS:
                return code
    return None


def _normalise(lang: str | None) -> str:
    """Reduce "es_ES.UTF-8" / "es-ES" -> "es"; lowercase the code."""
    if not lang:
        return ""
    code = lang.strip().replace("-", "_").split(".")[0].split("_")[0]
    return code.lower()
