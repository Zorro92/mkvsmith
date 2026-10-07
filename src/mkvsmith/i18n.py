"""
Internationalisation (i18n) for mkvsmith.

Uses the English source string itself as the lookup key (gettext-style), so any
untranslated phrase falls back to English automatically — no separate English
catalogue is needed, and adding a new user-facing string never breaks existing
locales.

Language resolution order (first match wins):
    1. --ui-lang flag            (explicit, overrides everything)
    2. settings file ("ui.language" in $XDG_CONFIG_HOME/mkvsmith/config.json)
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
    "No episodes detected on this disc": "No se detectaron episodios en este disco",
    "Ripping {n} episode(s)...": "Extrayendo {n} episodio(s)...",
    "Main feature: #{idx} {name} ({dur})": "Película principal: #{idx} {name} ({dur})",
    "\nSummary: {ok} ok, {fail} failed": "\nResumen: {ok} ok, {fail} fallidos",
    # --- interactive_mode ------------------------------------------------------
    "Invalid: {idx}": "No válido: {idx}",
    # --- multi-edition ---------------------------------------------------------
    "--multi-edition expects comma-separated title numbers": "--multi-edition espera números de título separados por comas",
    "--multi-edition needs at least two titles": "--multi-edition necesita al menos dos títulos",
    "Multi-edition titles must come from the same source disc": "Los títulos de multi-edición deben provenir del mismo disco de origen",
    "Multi-edition needs at least two titles": "Multi-edición necesita al menos dos títulos",
    "Edition {n}/{total}: {name} ({dur})": "Edición {n}/{total}: {name} ({dur})",
    "Combining {n} editions into one multi-edition MKV (requires a player with ordered-chapters support)": "Combinando {n} ediciones en un MKV multi-edición (requiere un reproductor con soporte de capítulos ordenados)",
    "{name} starts on a non-IDR video frame: some hardware decoders (e.g. on Android) show a black screen with audio. Play it with software decoding.": "{name} empieza en un fotograma de vídeo que no es IDR: algunos decodificadores por hardware (p. ej. en Android) muestran la pantalla en negro con audio. Reprodúcelo con decodificación por software.",
    "Skipping TMDB tagging for {name}: series discs aren't supported by movie tagging": "Se omite el etiquetado de TMDB para {name}: el etiquetado de películas no admite discos de series",
    "Detected {n} episode playlist(s)": "Se detectaron {n} playlist(s) de episodios",
    "Marked {name} as interlaced ({order})": "{name} marcado como entrelazado ({order})",
    "top field first": "campo superior primero",
    "bottom field first": "campo inferior primero",
    "Title {idx} holds {n} episodes in one playlist - split it with --split-episodes": "El título {idx} contiene {n} episodios en una sola playlist: divídelo con --split-episodes",
    "No playlist on this disc holds packed episodes": "Ninguna playlist de este disco contiene episodios empaquetados",
    "Title {idx} holds no packed episodes": "El título {idx} no contiene episodios empaquetados",
    "Split packed playlists into one title per episode": "Playlists empaquetadas divididas en un título por episodio",
    "split playlists holding several back-to-back episodes into one title per episode": "divide las playlists que contienen varios episodios seguidos en un título por episodio",
    "Edition {n}": "Edición {n}",
    "'{name}' lists different tracks than '{first}'; combining all tracks from both editions": "'{name}' lista pistas diferentes que '{first}'; combinando todas las pistas de ambas ediciones",
    "'{name}' shares too few clips with '{first}'; editions combined into one MKV must overlap": "'{name}' comparte muy pocos clips con '{first}'; las ediciones combinadas en un MKV deben solaparse",
    "'{name}' has a very different duration than '{first}'; editions combined into one MKV must be cuts of the same movie": "'{name}' tiene una duración muy diferente de '{first}'; las ediciones combinadas en un MKV deben ser cortes de la misma película",
    # --- display_titles --------------------------------------------------------
    "  SCANNED TITLES": "  TÍTULOS ESCANEADOS",
    "Dur": "Dur",
    "Name": "Nombre",
    "Playlist": "Lista",
    "PL": "PL",
    "Streams": "Pistas",
    "UPC/EAN: {value}": "UPC/EAN: {value}",
    "Disc Hash: {value}": "Disc Hash: {value}",
    "Fingerprint: {value}": "Huella: {value}",
    "Provider ID: {value}": "ID del proveedor: {value}",
    "DVD Disc ID: {value}": "ID del DVD: {value}",
    "libdvdread Disc ID: {value}": "Disc ID de libdvdread: {value}",
    "AACS Disc ID: {value}": "Disc ID de AACS: {value}",
    "TheDiscDB Disc Hash: {value}": "Disc Hash de TheDiscDB: {value}",
    "Matrix256 fingerprint: {value}": "Huella Matrix256: {value}",
    "Matrix256 fingerprint unavailable: the ISO listing did not report every file size": (
        "Huella Matrix256 no disponible: el listado de la ISO no informó todos los tamaños de archivo"
    ),
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
    "Using language: {name} ({code})": "Usando idioma: {name} ({code})",
    "Settings: {problem}": "Ajustes: {problem}",
    "This DVD is CSS-encrypted, so mkvsmith can't rip its titles. Decrypt it first (for example with a full disc backup) and rip the copy.\n\nShow its titles anyway? [y/N]:": "Este DVD está cifrado con CSS, así que mkvsmith no puede extraer sus títulos. Descífralo primero (por ejemplo, con una copia de seguridad completa del disco) y extrae la copia.\n\n¿Mostrar sus títulos de todos modos? [y/N]:",
    "⚠ This DVD is CSS-encrypted: its titles can't be ripped until it's decrypted.": "⚠ Este DVD está cifrado con CSS: sus títulos no se pueden extraer hasta descifrarlo.",
    "This DVD is CSS-encrypted; decrypt it first": "Este DVD está cifrado con CSS; descífralo primero",
    "This DVD is CSS-encrypted. mkvsmith can't decrypt it, so its titles can't be ripped; decrypt it first (for example with a full disc backup) and rip the copy.": "Este DVD está cifrado con CSS. mkvsmith no puede descifrarlo, así que sus títulos no se pueden extraer; descífralo primero (por ejemplo, con una copia de seguridad completa del disco) y extrae la copia.",
    "Title {idx} looks mis-authored: its angle blocks have angles of different lengths, so it plays parts of the film out of order (on players too)": "El título {idx} parece mal creado: sus bloques de ángulos tienen ángulos de distinta duración, así que reproduce partes de la película desordenadas (también en reproductores)",
    "PGC": "PGC",
    "VTS/PGC": "VTS/PGC",
    "SOURCE": "ORIGEN",
    "OUTPUT": "SALIDA",
    "%(prog)s [options] SOURCE [OUTPUT]": "%(prog)s [opciones] ORIGEN [SALIDA]",
    "open the interactive prompt": "abre el modo interactivo",
    "rip the main feature to ~/rips": "extrae la película principal en ~/rips",
    "rip titles 1 and 3, Japanese then English": "extrae los títulos 1 y 3, japonés y luego inglés",
    "change a saved setting": "cambia un ajuste guardado",
    "examples:": "ejemplos:",
    "Source and output": "Origen y salida",
    "Actions (pick one; none opens the interactive prompt)": "Acciones (elige una; sin ninguna se abre el modo interactivo)",
    "Tracks": "Pistas",
    "Titles": "Títulos",
    "Output files": "Archivos de salida",
    "TMDB tagging": "Etiquetado TMDB",
    "TheDiscDB": "TheDiscDB",
    "Temporary files": "Archivos temporales",
    "Saved settings (defaults for every run; list them with --settings)": "Ajustes guardados (predeterminados en cada ejecución; lístalos con --settings)",
    "Other": "Otros",
    "rip every listed title": "extrae todos los títulos listados",
    "scan the disc and list its titles": "escanea el disco y lista sus títulos",
    "show title N's tracks and chapters": "muestra las pistas y capítulos del título N",
    'preferred languages, most preferred first, e.g. jpn,eng or "jpn eng": keeps their subtitles and marks the default audio track': 'idiomas preferidos, el preferido primero, p. ej. jpn,eng o "jpn eng": conserva sus subtítulos y marca la pista de audio predeterminada',
    "keep audio in every language (--no-all-audio: only --languages)": "conserva el audio en todos los idiomas (--no-all-audio: solo --languages)",
    'save a setting and exit, e.g. tmdb.tagging=never or "temp.dir=/mnt/big disk/tmp"; with no value it asks (secrets typed hidden)': 'guarda un ajuste y sale, p. ej. tmdb.tagging=never o "temp.dir=/mnt/disco grande/tmp"; sin valor lo pregunta (los secretos se escriben ocultos)',
    "put a saved setting back to its default and exit": "devuelve un ajuste guardado a su valor predeterminado y sale",
    "show the common options and exit": "muestra las opciones habituales y sale",
    "show every option and exit": "muestra todas las opciones y sale",
    "show the version and exit": "muestra la versión y sale",
    "Run without an action to open the interactive prompt.": "Ejecútalo sin una acción para abrir el modo interactivo.",
    "More options (stream selection, captions, titles, TMDB tagging, TheDiscDB, temporary files): --help-all": "Más opciones (selección de pistas, subtítulos ocultos, títulos, etiquetado TMDB, TheDiscDB, archivos temporales): --help-all",
    "hide titles shorter than this many seconds (default: 60)": "oculta los títulos de menos de estos segundos (predeterminado: 60)",
    "print verbose debug output": "muestra salida de depuración detallada",
    "Unknown action: {action}": "Acción desconocida: {action}",
    "○ = episode": "○ = episodio",
    'disc, folder, or image to read; quote paths with spaces, e.g. "/media/My Disc.iso"': 'disco, carpeta o imagen a leer; pon entre comillas las rutas con espacios, p. ej. "/media/Mi Disco.iso"',
    'output directory (default: current directory), e.g. "/media/rips/New Movies"': 'directorio de salida (predeterminado: el directorio actual), p. ej. "/media/rips/Películas nuevas"',
    'rip title N (or several: 1,3,5 or "1 3 5")': 'extrae el título N (o varios: 1,3,5 o "1 3 5")',
    'combine versions of one film (Blu-ray playlists or DVD chains) into one multi-edition MKV, e.g. 1,2 or "1 2"': 'combina versiones de una película (playlists de Blu-ray o cadenas de DVD) en un MKV multiedición, p. ej. 1,2 o "1 2"',
    'streams to rip, e.g. v:0,a:eng,s:all or "v:0 a:eng s:all"': 'pistas a extraer, p. ej. v:0,a:eng,s:all o "v:0 a:eng s:all"',
    "closed-caption sidecar format: srt (portable plain text) or ass (preserves speaker positioning and italics)": "formato del archivo de subtítulos ocultos: srt (texto plano portátil) o ass (conserva la posición de los hablantes y las cursivas)",
    'metadata properties to fetch (default: a sensible set), e.g. Title,Overview or "Title Overview"': 'propiedades de metadatos a obtener (predeterminado: un conjunto razonable), p. ej. Title,Overview o "Title Overview"',
    'override the movie title used for the TMDB search, e.g. "The Matrix"': 'sustituye el título de película usado en la búsqueda de TMDB, p. ej. "The Matrix"',
    'output directory for TheDiscDB contribution files, e.g. "/media/rips/DiscDB bundles"': 'directorio de salida para los archivos de contribución de TheDiscDB, p. ej. "/media/rips/Paquetes DiscDB"',
    'disc name for a direct TheDiscDB contribution (default: Disc 1), e.g. "Bonus Disc"': 'nombre del disco para una contribución directa a TheDiscDB (predeterminado: Disc 1), p. ej. "Disco extra"',
    'authenticated TheDiscDB browser cookie (or set THEDISCDB_COOKIE); quote it, e.g. "name=value; other=value"': 'cookie autenticada del navegador para TheDiscDB (o define THEDISCDB_COOKIE); ponla entre comillas, p. ej. "nombre=valor; otro=valor"',
    "Series disc: no single main feature; ripping all episodes": "Disco de serie: no hay una película principal; se extraen todos los episodios",
    "Invalid title for {action}: {idx}": "Título no válido para {action}: {idx}",
    "No multi-edition title indexes supplied": "No se indicaron títulos para la multiedición",
    'Set explicitly to use tmpfs/RAM (see --ram-limit) or another disk path, e.g. "/mnt/big disk/tmp".': 'Indícalo para usar tmpfs/RAM (ver --ram-limit) u otra ruta en disco, p. ej. "/mnt/disco grande/tmp".',
    "TheDiscDB returned unusable match data; local metadata retained: {err}": "TheDiscDB devolvió datos de coincidencia inutilizables; se conservan los metadatos locales: {err}",
    "TheDiscDB results were ambiguous; local metadata retained": "Los resultados de TheDiscDB eran ambiguos; se conservan los metadatos locales",
    "TheDiscDB disc matched, but no local title correlated uniquely; local metadata retained": "El disco coincidió en TheDiscDB, pero ningún título local se correspondió de forma única; se conservan los metadatos locales",
    "Opened TheDiscDB contribution flow in your browser": "Se abrió el flujo de contribución de TheDiscDB en tu navegador",
    "Could not open a browser; continue at {url}": "No se pudo abrir un navegador; continúa en {url}",
    "Matrix256 fingerprint unavailable: {err}": "Huella Matrix256 no disponible: {err}",
    "Direct TheDiscDB contribution requires an authenticated cookie": "La contribución directa a TheDiscDB requiere una cookie autenticada",
    "Direct TheDiscDB contribution requires a contribution ID": "La contribución directa a TheDiscDB requiere un ID de contribución",
    "Detected {n} episode(s) across {m} title(s)": "Se detectaron {n} episodio(s) en {m} título(s)",
    "Dropping {n} episode label(s): dwarfed by a {dur} title — bonus content on a movie disc": "Se descartan {n} etiqueta(s) de episodio: eclipsadas por un título de {dur}; contenido extra en un disco de película",
    "Settings file: {path}": "Archivo de ajustes: {path}",
    "expected a comma-separated list": "se esperaba una lista separada por comas",
    "expected comma-separated title numbers": "se esperaban números de título separados por comas",
    "show the saved settings and exit": "muestra los ajustes guardados y sale",
    "keep subtitle tracks": "conserva las pistas de subtítulos",
    "keep subtitles in every language, not only the preferred ones": "conserva los subtítulos en todos los idiomas, no solo en los preferidos",
    "keep forced subtitle tracks": "conserva las pistas de subtítulos forzados",
    "extract EIA-608 closed captions as a text subtitle track (format: --cc-format)": "extrae los subtítulos ocultos EIA-608 como pista de subtítulos de texto (formato: --cc-format)",
    "an output file that already exists: ask first, always overwrite, or never (skip the title)": "un archivo de salida que ya existe: preguntar, sobrescribir siempre o nunca (omitir el título)",
    "same as --overwrite always": "igual que --overwrite always",
    "TMDB API key for this run (visible to other users; prefer TMDB_API_KEY or --set tmdb.api_key)": "clave de API de TMDB para esta ejecución (visible para otros usuarios; mejor TMDB_API_KEY o --set tmdb.api_key)",
    "cover art to embed from TMDB (ask: the interactive prompt asks per rip)": "arte de portada de TMDB a incrustar (ask: el modo interactivo pregunta en cada extracción)",
    "confirm the TMDB match before tagging each rip": "confirma la coincidencia de TMDB antes de etiquetar cada extracción",
    "write a TheDiscDB contribution bundle: browser, manual, authenticated direct, or off": "escribe un paquete de contribución para TheDiscDB: browser, manual, direct (autenticado) u off",
    "{key} = {value}": "{key} = {value}",
    "{key} reset to its default": "{key} vuelve a su valor predeterminado",
    "(default, not chosen yet)": "(predeterminado, aún sin elegir)",
    "{err} (see --settings for every key)": "{err} (ver --settings para todas las claves)",
    "--set/--reset can't be combined with an action": "--set/--reset no se pueden combinar con una acción",
    "Keep subtitles in every language, not only the preferred languages?": "¿Conservar los subtítulos en todos los idiomas, no solo en los preferidos?",
    "Hide titles shorter than this many seconds": "Ocultar los títulos de menos de estos segundos",
    "Show low-quality titles too (menus, trailers, etc.)?": "¿Mostrar también los títulos de baja calidad (menús, tráileres, etc.)?",
    "Directory for temporary files (empty: /var/tmp when usable)": "Directorio para archivos temporales (vacío: /var/tmp si se puede usar)",
    "Max fraction of RAM-backed temp space to use (0 disables the check)": "Fracción máxima del espacio temporal en RAM a usar (0 desactiva la comprobación)",
    "TMDB metadata properties to fetch": "Propiedades de metadatos de TMDB a obtener",
    "ISO 3166-1 region for the content rating": "Región ISO 3166-1 para la clasificación por edades",
    "TMDB language for localized metadata (empty: TMDB's default)": "Idioma de TMDB para metadatos localizados (vacío: el predeterminado de TMDB)",
    "Keep the XML tag file after muxing?": "¿Conservar el archivo XML de etiquetas tras el muxing?",
    "TheDiscDB base URL": "URL base de TheDiscDB",
    "Write a TheDiscDB contribution bundle (off, browser, manual, direct)": "Escribir un paquete de contribución para TheDiscDB (off, browser, manual, direct)",
    "Open TheDiscDB in a browser after preparing a contribution?": "¿Abrir TheDiscDB en el navegador tras preparar una contribución?",
    "Settings saved to {path}": "Ajustes guardados en {path}",
    "yes": "sí",
    "no": "no",
    "Value": "Valor",
    "Invalid value: {err}": "Valor no válido: {err}",
    "never": "nunca",
    "ask every time": "preguntar cada vez",
    "always": "siempre",
    "none": "ninguno",
    "poster": "póster",
    "backdrop": "imagen de fondo",
    "poster and backdrop": "póster e imagen de fondo",
    "Interface language": "Idioma de la interfaz",
    "Overwrite an output file that already exists?": "¿Sobrescribir un archivo de salida que ya existe?",
    "Preferred audio/subtitle languages, most preferred first": "Idiomas preferidos de audio/subtítulos, el preferido primero",
    "Keep every audio track, not only the preferred languages?": "¿Conservar todas las pistas de audio, no solo las de los idiomas preferidos?",
    "Keep subtitle tracks?": "¿Conservar las pistas de subtítulos?",
    "Keep forced subtitle tracks?": "¿Conservar las pistas de subtítulos forzados?",
    "Extract DVD closed captions (EIA-608) as a text subtitle track?": "¿Extraer los subtítulos ocultos del DVD (EIA-608) como pista de subtítulos de texto?",
    "Closed-caption format (srt: plain text; ass: keeps positioning and italics)": "Formato de subtítulos ocultos (srt: texto plano; ass: conserva posición y cursivas)",
    "Split playlists holding back-to-back episodes into one title each?": "¿Dividir las playlists con episodios consecutivos en un título por episodio?",
    "TMDB API key (optional, enables tagging)": "Clave de API de TMDB (opcional, habilita el etiquetado)",
    "Tag rips with TMDB metadata?": "¿Etiquetar las extracciones con metadatos de TMDB?",
    "Confirm the TMDB match before tagging?": "¿Confirmar la coincidencia de TMDB antes de etiquetar?",
    "Cover art to attach": "Arte de portada a adjuntar",
    "Look discs up on TheDiscDB (real episode numbers and titles)?": "¿Buscar los discos en TheDiscDB (números y títulos reales de episodios)?",
    "Output folder": "Carpeta de salida",
    "Cannot use {path}: {err}": "No se puede usar {path}: {err}",
    "Title {idx} holds {n} episodes in one playlist. Split it into one title per episode? [y/N]": "El título {idx} contiene {n} episodios en una playlist. ¿Dividirlo en un título por episodio? [y/N]",
    "This disc has closed captions. Add them as a subtitle track? [y/N]": "Este disco tiene subtítulos ocultos. ¿Añadirlos como pista de subtítulos? [y/N]",
    # --- tagger prompts --------------------------------------------------------
    "Look up & tag this rip on TMDB?": "¿Buscar y etiquetar esta extracción en TMDB?",
    "Attach artwork?": "¿Adjuntar arte?",
    "None": "Ninguno",
    "Poster": "Póster",
    "Backdrop": "Imagen de fondo",
    "Both": "Ambos",
    "Property": "Propiedad",
    "What would you like to do?": "¿Qué quieres hacer?",
    "Rip a disc": "Extraer un disco",
    "Settings": "Ajustes",
    "About / keys": "Acerca de / teclas",
    "Back": "Atrás",
    "Quit": "Salir",
    "Choose a disc": "Elige un disco",
    "Looking for drives...": "Buscando unidades...",
    "No optical drives found": "No se encontraron unidades ópticas",
    "No disc": "Sin disco",
    "Disc inserted": "Disco insertado",
    "Checking...": "Comprobando...",
    "Browse for an ISO or disc folder...": "Buscar una ISO o carpeta de disco...",
    "Refresh": "Actualizar",
    "Eject": "Expulsar",
    "Could not eject {path}: {err}": "No se pudo expulsar {path}: {err}",
    "Up": "Subir",
    "Type a path": "Escribir una ruta",
    "Use this folder": "Usar esta carpeta",
    "Use this folder ({kind})": "Usar esta carpeta ({kind})",
    "Folder, ISO or video file": "Carpeta, ISO o archivo de vídeo",
    "Not a folder, ISO or video file: {path}": "No es una carpeta, ISO ni archivo de vídeo: {path}",
    "OK": "Aceptar",
    "The interactive mode needs a terminal; rip with -t, -m or -a, or list titles with -i": "El modo interactivo necesita una terminal; extrae con -t, -m o -a, o lista los títulos con -i",
    "Name of edition {n}": "Nombre de la edición {n}",
    "{n} title(s)": "{n} título(s)",
    "Done: {ok} ok, {fail} failed": "Hecho: {ok} correctos, {fail} con errores",
    "Muxing... {pct}%": "Multiplexando... {pct}%",
    "Ripping {pos} of {total}": "Extrayendo {pos} de {total}",
    "Preparing...": "Preparando...",
    "Done: {name} ({size})": "Hecho: {name} ({size})",
    "Stopping...": "Deteniendo...",
    "  Space            mark a title / toggle a track": "  Espacio          marcar un título / activar una pista",
    "New settings to choose since your last run": "Ajustes nuevos para elegir desde la última vez",
    "No": "No",
    "Yes": "Sí",
    "Scanning {path}...": "Escaneando {path}...",
    "custom tracks": "pistas personalizadas",
    "{n} hidden": "{n} ocultos",
    "Rip all listed titles ({n})": "Extraer todos los títulos de la lista ({n})",
    "Mark": "Marcar",
    "{n} chapter(s)": "{n} capítulo(s)",
    "Rip this title": "Extraer este título",
    "Video": "Vídeo",
    "Audio": "Audio",
    "Subtitles": "Subtítulos",
    "Toggle": "Activar",
    "Ripping {n} title(s)": "Extrayendo {n} título(s)",
    "Stopped": "Detenido",
    "mkvmerge stopped responding (no progress for {minutes} minutes)": (
        "mkvmerge dejó de responder (sin progreso durante {minutes} minutos)"
    ),
    "Failed: {err}": "Error: {err}",
    "Stop ripping? The title being ripped is deleted.": "¿Detener la extracción? Se borrará el título que se está extrayendo.",
    "Stop ripping and quit? The title being ripped is deleted.": "¿Detener la extracción y salir? Se borrará el título que se está extrayendo.",
    "Scan failed: {err}": "Error al escanear: {err}",
    "Rip marked titles ({n})": "Extraer los títulos marcados ({n})",
    "Rip all episodes ({n})": "Extraer todos los episodios ({n})",
    "Combine titles {idxs} into one multi-edition MKV": "Combinar los títulos {idxs} en un MKV multiedición",
    "Show {n} hidden titles": "Mostrar {n} títulos ocultos",
    "Reset tracks to the defaults": "Restablecer las pistas predeterminadas",
    "Choose at least one track": "Elige al menos una pista",
    "Ripping stopped: {err}": "Extracción detenida: {err}",
    "Skipped": "Omitido",
    "Combine marked titles into one multi-edition MKV": "Combinar los títulos marcados en un MKV multiedición",
    "Split title {idx} into its {n} episodes": "Dividir el título {idx} en sus {n} episodios",
    "Hide short and duplicate titles": "Ocultar títulos cortos y duplicados",
    "Marked for a batch rip": "Marcado para extraer en lote",
    "Rip main feature: {name}": "Extraer la película principal: {name}",
    "Waiting": "En espera",
    "(disc)": "(disco)",
    "Loading...": "Cargando...",
    "Can't read this folder: {err}": "No se puede leer esta carpeta: {err}",
    "No folders, ISOs or video files here": "Aquí no hay carpetas, ISO ni archivos de vídeo",
    "Saved settings: the defaults for every disc": "Ajustes guardados: los valores por defecto para cada disco",
    "Automatic": "Automático",
    "Cancel": "Cancelar",
    "Default": "Predeterminado",
    "mkvmerge: {path}": "mkvmerge: {path}",
    "not found": "no encontrado",
    "Interface language: {name}": "Idioma de la interfaz: {name}",
    "Keys": "Teclas",
    "  Arrows / wheel   move": "  Flechas / rueda  moverse",
    "  Enter / click    choose": "  Intro / clic     elegir",
    "  Esc              go back": "  Esc              volver",
    "  q                quit": "  q                salir",
    "Multiple TMDB matches for '{title}':": "Varias coincidencias en TMDB para '{title}':",
    "Choose": "Elegir",
    "Metadata Preview": "Vista previa de metadatos",
    "Could not write config: {err}": "No se pudo escribir la configuración: {err}",
    "Could not write tag XML: {err}": "No se pudo escribir el XML de etiquetas: {err}",
    "Could not update tags in {name}: {err}": "No se pudieron actualizar las etiquetas en {name}: {err}",
    "Could not update tags in {name} (mkvpropedit failed)": "No se pudieron actualizar las etiquetas en {name} (falló mkvpropedit)",
    "Tagging failed (ripping without tags): {err}": "El etiquetado falló (extrayendo sin etiquetas): {err}",
    # --- muxing ----------------------------------------------------------------
    "Muxing: {name}...": "Multiplexando: {name}...",
    "Created: {name} ({size:.1f} {unit})": "Creado: {name} ({size:.1f} {unit})",
    # --- scan status -----------------------------------------------------------
    "Source type: {type}": "Tipo de origen: {type}",
    "Using ISO file in directory: {name}": "Usando archivo ISO del directorio: {name}",
    "No ISO file found in {path}": "No se encontró ningún archivo ISO en {path}",
    "{path} is not a valid disc image (no ISO9660 or UDF filesystem found)": "{path} no es una imagen de disco válida (no se encontró sistema de archivos ISO9660 o UDF)",
    "Scanning ISO...": "Escaneando ISO...",
    "Could not find any .mpls, .m2ts, .vob, or .evo files inside the ISO.": "No se encontró ningún archivo .mpls, .m2ts, .vob o .evo dentro de la ISO.",
    "HD DVD playlist: {name} ({n} title(s))": "Lista HD DVD: {name} ({n} título(s))",
    "Disc name from bdmt.xml: {name}": "Nombre del disco desde bdmt.xml: {name}",
    "Disc name: {name}": "Nombre del disco: {name}",
    "VMG disc name: {name}": "Nombre del disco VMG: {name}",
    "Detected DVD VIDEO_TS structure in ISO": "Estructura DVD VIDEO_TS detectada en la ISO",
    "Detected {n} episode(s) in VTS {vts}": "Se detectaron {n} episodio(s) en el VTS {vts}",
    "mkvmerge not available, cannot scan video file.": "mkvmerge no disponible, no se puede escanear el archivo de vídeo.",
    "Device read failed (needs libdvdcss/libaacs)": "Error de lectura del dispositivo (necesita libdvdcss/libaacs)",
    # --- disc_reader ------------------------------------------------------------
    "Skipping incompatible clip for append: {name} (audio layout differs)": "Omitiendo clip incompatible para anexar: {name} (el audio difiere)",
    "Extracting {n} files from ISO ({size} GB); this may take a while...": "Extrayendo {n} archivos del ISO ({size} GB); esto puede tardar un rato...",
    "Extracting {n} files from ISO; this may take a while...": "Extrayendo {n} archivos del ISO; esto puede tardar un rato...",
    "Verifying track layouts across {n} clips...": "Verificando las pistas en {n} clips...",
    "Checking {n} clips for append compatibility...": "Comprobando {n} clips para compatibilidad de anexión...",
    "Scanning seamless joints across {n} clips...": "Analizando las uniones sin fisuras en {n} clips...",
    "Skipping {stream}: it is stored in a Blu-ray sub-path clip, which can't be muxed": "Omitiendo {stream}: está en un clip de sub-ruta del Blu-ray, que no se puede multiplexar",
    "(sub-path, not muxed)": "(sub-ruta, no se multiplexa)",
    "Could not read clip offsets from {name}; edition chapters may be misaligned at branch points": "No se pudieron leer los desfases de clips de {name}; los capítulos de las ediciones pueden quedar desalineados en los puntos de bifurcación",
    "Temp dir '{dir}' is RAM-backed; limiting extracts to {gb:.1f} GB "
    "({pct:.0%} of {total_gb:.1f} GB {kind}). Oversized titles spill to disk.": "El directorio temporal '{dir}' está en RAM; limitando las extracciones "
    "a {gb:.1f} GB ({pct:.0%} de {total_gb:.1f} GB de {kind}). Los títulos "
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
    "Title estimated at {est:.1f} GB fits the RAM budget of {budget:.1f} GB "
    "but the temp filesystem is low on space ({avail:.1f} GB free); "
    "using disk-backed temp '{dir}' for this title.": "El título, estimado en {est:.1f} GB, cabe en el presupuesto de RAM de "
    "{budget:.1f} GB pero el sistema de archivos temporal tiene poco espacio "
    "({avail:.1f} GB libres); usando el directorio temporal en disco '{dir}' "
    "para este título.",
    "Removed {count} leftover temp folder(s) ({gb:.1f} GB) from interrupted runs.": "Se eliminaron {count} carpeta(s) temporal(es) sobrante(s) ({gb:.1f} GB) de ejecuciones interrumpidas.",
    "Could not read ISO image {path}: {err}": "No se pudo leer la imagen ISO {path}: {err}",
    "File not found inside the ISO: {path}": "Archivo no encontrado dentro de la ISO: {path}",
    "Could not extract {path} from the ISO: {err}": "No se pudo extraer {path} de la ISO: {err}",
    "'{name}' already exists. Overwrite? [y/N]:": "'{name}' ya existe. ¿Sobrescribir? [y/N]:",
    # --- misc ------------------------------------------------------------------
    "Not found: {path}": "No encontrado: {path}",
    # --- argparse / --help -----------------------------------------------------
    "DVD/Blu-ray ripper using mkvmerge (MKVToolNix)": "Extractor de DVD/Blu-ray usando mkvmerge (MKVToolNix)",
    "rip only the detected main feature": "extraer solo la película principal detectada",
    "rip the detected main feature (all episodes on series discs)": "extraer la película principal detectada (todos los episodios en discos de series)",
    "rip all detected TV-series episodes": "extraer todos los episodios de series detectados",
    "--episodes is deprecated; use --main (episodes on series discs)": "--episodes está obsoleto; usa --main (episodios en discos de series)",
    "rip the given playlist titles as ONE multi-edition MKV (comma-separated title numbers, first is the default edition); e.g. --multi-edition 0,1,2": "extrae los títulos de playlist indicados como UN MKV multi-edición (números de título separados por comas, el primero es la edición predeterminada); p. ej. --multi-edition 0,1,2",
    "show all titles including low-quality ones (menus, trailers, etc.)": "mostrar todos los títulos, incluidos los de baja calidad (menús, tráilers, etc.)",
    "directory for temporary files (default: /var/tmp when usable, else system temp). ": "directorio para archivos temporales (predeterminado: /var/tmp si está disponible, si no el tmp del sistema). ",
    "max fraction of RAM-backed (tmpfs) temp capacity that extractions may "
    "use before spilling to disk (default: 0.8). 0 disables the check.": "fracción máxima de la capacidad temporal en "
    "RAM (tmpfs) que las extracciones pueden usar antes de pasar al disco "
    "(predeterminado: 0.8). 0 desactiva la comprobación.",
    "overwrite existing output files without asking": "sobrescribir los archivos de salida existentes sin preguntar",
    "do not tag, even in interactive mode when a TMDB key is available": "no etiquetar, ni siquiera en modo interactivo cuando hay una clave de TMDB",
    "fetch TMDB metadata and tag each rip during muxing": "obtener metadatos de TMDB y etiquetar cada extracción durante el muxado",
    "TMDB API key (or set TMDB_API_KEY, or store with --save-key)": "clave de API de TMDB (o define TMDB_API_KEY, o guárdala con --save-key)",
    "store the TMDB API key to the config file and exit": "guardar la clave de API de TMDB en el archivo de configuración y salir",
    "ISO 3166-1 region for content rating (default: US)": "región ISO 3166-1 para la clasificación de contenido (predeterminado: US)",
    "TMDB language code for localized metadata (e.g. en, ja, fr)": "código de idioma de TMDB para metadatos localizados (p. ej. en, ja, fr)",
    "download and embed cover art from TMDB into the MKV": "descargar e incrustar arte de portada de TMDB en el MKV",
    "keep the XML tag file after muxing": "conservar el archivo XML de etiquetas después del muxado",
    "skip the per-rip tagging confirmation prompt": "omitir el mensaje de confirmación de etiquetado por extracción",
    "override the release year used for the TMDB search": "sobrescribir el año de estreno usado en la búsqueda de TMDB",
    "query TheDiscDB and automatically apply a unique disc match": "consultar TheDiscDB y aplicar automáticamente una coincidencia única de disco",
    "TheDiscDB base URL (or set THEDISCDB_BASE_URL)": "URL base de TheDiscDB (o define THEDISCDB_BASE_URL)",
    "TheDiscDB network timeout in seconds": "tiempo de espera de red de TheDiscDB en segundos",
    "write a TheDiscDB contribution bundle (browser, manual, or authenticated direct)": "escribir un paquete de contribución de TheDiscDB (browser, manual o direct autenticado)",
    "open TheDiscDB in a browser after preparing a contribution": "abrir TheDiscDB en un navegador después de preparar una contribución",
    "existing TheDiscDB contribution ID for browser/direct handoff": "ID de contribución existente de TheDiscDB para la transición con navegador/directa",
    "No TheDiscDB match found": "No se encontró coincidencia en TheDiscDB",
    "Multiple TheDiscDB disc layouts matched; local metadata retained": "Varias distribuciones de disco de TheDiscDB coinciden; se conservan los metadatos locales",
    "TheDiscDB lookup failed (using local metadata): {err}": "Falló la consulta a TheDiscDB (usando metadatos locales): {err}",
    "TheDiscDB match: {media} ({release}); applied {count} title name(s)": "Coincidencia de TheDiscDB: {media} ({release}); se aplicaron {count} nombre(s) de título",
    "TheDiscDB contribution bundle written to {path}": "Paquete de contribución de TheDiscDB escrito en {path}",
    "TheDiscDB contribution disc uploaded: {url}": "Disco de contribución subido a TheDiscDB: {url}",
    "TheDiscDB contribution failed: {err}": "Falló la contribución a TheDiscDB: {err}",
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

_active_lang = "en"
_active_catalog: dict[str, str] = {}


# =============================================================================
# API
# =============================================================================


def set_language(lang: str | None) -> str:
    """Activate *lang* (e.g. "es", "en"). Falls back to "en".

    Accepts full locale strings like "es_ES.UTF-8" (the part before "." and "_"
    is taken as the language code). Returns the resolved code actually applied.
    """
    global _active_lang, _active_catalog
    code = _normalise(lang)
    if code not in _TRANSLATIONS and code != "en":
        # Unknown / unsupported language -> English.
        code = "en"
    _active_lang = code
    _active_catalog = _TRANSLATIONS.get(code, {})
    return code


def get_language() -> str:
    return _active_lang


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
    out = _active_catalog.get(text, text)
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
