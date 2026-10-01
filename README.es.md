# mkvsmith

> [English](README.md) · [Español](README.es.md)

Extractor de DVD/Blu-ray que produce archivos MKV usando
[mkvmerge](https://mkvtoolnix.download/) (MKVToolNix).

`mkvsmith` lee las estructuras del disco directamente — metadatos
`.mpls` / `.clpi` / `.ifo` / BDMV — en lugar de sondear los flujos de medios.
Eso hace que el escaneo sea rápido y con pocas dependencias: la única
herramienta de medios externa que necesita es `mkvmerge`. Sigue las convenciones
de extracción de facto cuando son el valor por defecto más sensato, pero es
una reimplementación independiente con licencia GPL.

## Características

- **Extrae discos DVD (VIDEO_TS) y Blu-ray (BDMV), ISOs, archivos
  `.m2ts`/`.vob` sueltos y archivos de vídeo normales** a Matroska (`.mkv`).
- **Extrae discos e ISOs HD DVD** (`HVDVD_TS/*.evo` + listas XPL, con
  capítulos, idiomas XPL y extracción de subimágenes EVO).
- **Conserva el audio, los subtítulos y los capítulos**, incluidos los flujos
  de subimagen de DVD que otros escáneres más simples pasan por alto.
- **Escribe etiquetas `SOURCE_ID` por pista** (PID de
  Blu-ray / IDs de flujo+VTS de DVD) junto a las etiquetas estadísticas de
  mkvmerge.
- **Muestra identificadores estables del disco**, incluida la huella
  [matrix256v1](https://github.com/shitwolfymakes/matrix256) del sistema de
  archivos y los identificadores de metadatos DVD/Blu-ray.
- **Consulta opcional de [TheDiscDB](https://thediscdb.com/)** — identifica
  discos, aplica nombres comunitarios y resuelve playlists principales
  ofuscadas.
- **Etiquetado TMDB opcional** — metadatos y carátulas incrustados directamente
  en el mux.

## Requisitos

- **Python 3.12+**
- **mkvmerge** (MKVToolNix) — la única herramienta externa y un requisito
  imprescindible para el multiplexado. Las imágenes ISO (UDF e ISO9660) se
  leen de forma nativa.
- **libdvdcss / libaacs** — necesarios para que tu sistema lea discos
  comerciales *cifrados* (igual que cualquier extractor). `mkvsmith` no incluye
  ni elude DRM.

## Instalación

`mkvsmith` es un script de un solo archivo más unos cuantos módulos. La forma
más fácil de ejecutarlo es con [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/Zorro92/mkvsmith
cd mkvsmith
uv run ./main.py --help
```

`main.py` también lleva un shebang `uv run --script`, así que una vez que sea
ejecutable se puede lanzar directamente:

```sh
chmod +x main.py
./main.py --help
```

## Uso

```sh
# escanea una carpeta de disco / ISO y entra en modo interactivo
uv run ./main.py /ruta/al/disco
uv run ./main.py pelicula.iso

# extrae la película principal directamente al directorio actual
uv run ./main.py /ruta/al/disco -m

# extrae un título concreto
uv run ./main.py /ruta/al/disco -t 1

# extrae todos los títulos
uv run ./main.py /ruta/al/disco -a

# -m es inteligente: película principal en cine, episodios en series
uv run ./main.py /ruta/al/disco -m

# escribe la salida en un directorio concreto (segundo argumento posicional)
uv run ./main.py /ruta/al/disco -m ~/rips
```

### Modo interactivo

Ejecuta sin `-t/-m/-a` para entrar en el prompt interactivo:

```text
mkvsmith> n          # muestra los detalles del título n
mkvsmith> r 1        # extrae el título 1
mkvsmith> rm         # extrae la película principal (episodios en series)
mkvsmith> ra         # extrae todos los títulos
mkvsmith> q          # salir
```

### Opciones comunes

| Opción | Descripción |
|---|---|
| `-t, --title N` | Extrae un título concreto |
| `-m, --main` | Extrae la película principal detectada (todos los episodios en series) |
| `-a, --all` | Extrae todos los títulos |
| `-i, --info` | Solo escanea y lista los títulos |
| `--split-episodes` | Divide las playlists que contienen varios episodios seguidos en un título por episodio (desactivado por defecto) |
| `-s, --streams` | Selecciona pistas (p. ej. `v:0 a:eng s:all`) |
| `-l, --lang` | Idiomas preferidos (por defecto `eng,en,und`) |
| `--all-audio` / `--no-all-audio` | Conserva todo el audio (activado por defecto) |
| `--no-subs` | Descarta los subtítulos |
| `--no-forced` | Descarta los subtítulos forzados |
| `--min-duration N` | Ignora los títulos de menos de N segundos |
| `--show-all` | Muestra los títulos de baja calidad (menús/tráileres) |
| `--temp-dir DIR` | Directorio temporal (predeterminado: /var/tmp; para tmpfs/RAM u otra ruta en disco) |
| `--ram-limit FRAC` | Fracción máxima de capacidad tmpfs para directorios temporales en RAM |
| `--force` | Sobrescribir los archivos de salida existentes sin preguntar |
| `--tag` / `--no-tag` | Controles de etiquetado TMDB |
| `--discdb` / `--no-discdb` | Consulta de TheDiscDB (participativa) |
| `--discdb-contribute[=MODO]` | Prepara o sube una contribución (`browser`, `manual` o `direct`) |
| `--discdb-disc-name NOMBRE` | Nombre de disco usado en el modo directo |
| `--ui-lang LANG` | Idioma de la interfaz (p. ej. `en`, `es`) |
| `--debug` | Registro de depuración detallado |

### TheDiscDB

La consulta a TheDiscDB está desactivada por defecto. Actívala con `--discdb` o
persistela en `$XDG_CONFIG_HOME/mkvsmith/config.json` (por defecto `~/.config/...`)
bajo `"discdb": {"enabled": true}`. La consulta envía solo identificadores del
disco; nunca envía datos de playlists ni contenidos multimedia.

```sh
# identifica un disco y aplica una correspondencia única de títulos
uv run ./main.py pelicula.iso --discdb --info

# prepara archivos para el flujo de contribución revisado de TheDiscDB
uv run ./main.py pelicula.iso --discdb-contribute=browser
uv run ./main.py pelicula.iso --discdb-contribute=manual --discdb-bundle-dir ~/discdb
```

La correspondencia usa el Disc Hash heredado de TheDiscDB, la huella
Matrix256, el Disc ID AACS de Blu-ray o el Disc ID libdvdread de DVD. El
UPC/EAN es solo una pista débil y debe corroborarse con la playlist o con el
título y la duración. Un `MainMovie` remoto único tiene
prioridad sobre las heurísticas locales, lo que resuelve la ofuscación
«screen pass» sin adivinar. Las coincidencias ambiguas nunca renombran títulos
ni cambian la detección de la película principal.
Los Disc ID específicos del formato requieren estructuras `AACS`/`VIDEO_TS`
legibles (carpeta, ISO o imagen montada); el respaldo directo de `/dev` no los
expone.

`--discdb-contribute` escribe `manifest.json` y un registro de escaneo
compatible (`scan_log.txt`) generado desde el propio análisis de MPLS/CLPI/IFO de
mkvsmith. En el modo browser, abre o crea un borrador de contribución y sube
`scan_log.txt` donde el sitio pida un registro de escaneo.
El modo directo adjunta esos datos a un borrador existente con
`--discdb-contribution-id` y una cookie autenticada de navegador proporcionada
con `--discdb-cookie` o `THEDISCDB_COOKIE`; se detiene antes del etiquetado y
la revisión, que siguen siendo pasos aprobados por personas. Trata la cookie
como una contraseña. Usa `--discdb-disc-name` al añadir discos adicionales al
mismo borrador.
Los mapas de segmentos de Blu-ray provienen directamente de los IDs de clip de
la playlist; los mapas de rangos de celdas DVD se dejan en blanco para
identificación humana porque mkvsmith no expone IDs de celdas DVD.

## Notas

- **Soporte de plataformas:** Linux es la plataforma principal. Las fuentes
  de carpetas, ISO y archivos de vídeo están escritas para ser
  multiplataforma, y las letras de unidad de Windows (`E:`) se reconocen como
  fuentes de dispositivo, pero la entrada de dispositivos ópticos `/dev/...`
  es exclusiva de Linux. El soporte de Windows y macOS no está probado.
- Las imágenes ISO se leen con un lector UDF/ISO9660 integrado. Las imágenes
  de discos grabables o regrabables (particiones UDF «sparable» o virtuales)
  aún no son compatibles; copia esos discos a una carpeta primero.
- Los discos comerciales cifrados necesitan `libdvdcss` (DVD) / `libaacs`
  (Blu-ray) a nivel de sistema.
- Los archivos temporales usan `/var/tmp` (en disco) por defecto cuando está
  disponible, y si no el directorio temporal del sistema. Si el directorio
  temporal efectivo está respaldado en RAM (tmpfs — p. ej. un `--temp-dir /tmp`
  explícito), `mkvsmith` lo detecta y vuelca de forma transparente las
  extracciones demasiado grandes a disco. El presupuesto es `--ram-limit` del
  menor entre la RAM total y el tamaño del tmpfs (un tmpfs suele estar limitado
  a una fracción de la RAM), con comprobaciones adicionales para la RAM
  disponible y el espacio libre del tmpfs, ya que `/tmp` es compartido.
- **La salida MKV multi-edición** (`--multi-edition`, o el comando interactivo
  `me`) combina playlists con ramificación continua en un solo archivo con
  uniones sin pausas y capítulos exactos. Requiere un reproductor con
  soporte de capítulos ordenados (p. ej. mpv, VLC) para cambiar de edición.
- **Episodios empaquetados.** Algunos Blu-ray de series (p. ej. Sgt. Frog)
  reproducen todos los episodios del disco desde una sola playlist de 15-20
  horas. `mkvsmith` los detecta a partir de las marcas de capítulo y lista la
  playlist como de costumbre, con un aviso; por defecto no se divide nada.
  Usa `--split-episodes`, o el comando interactivo `se N`, para obtener un
  título por episodio (más cualquier extra final), cada uno recortado a su
  propio rango y con sus propios capítulos.
- **Dolby Vision no ha sido probado a fondo.** HDR10 y HDR10+ no requieren
  tratamiento especial (sus metadatos viajan dentro del bitstream de vídeo y
  sobreviven intactos a un remux), y la señalización de color BT.2020/PQ para
  Blu-rays HDR y DV se analiza desde la playlist y está cubierta por pruebas
  unitarias. Sin embargo, no se ha dispuesto de ningún disco Dolby Vision
  Profile 7 (UHD Blu-ray de doble capa) para hacer pruebas: un remux conserva
  únicamente la capa base compatible con HDR10 (el DV completo requeriría
  procesamiento a nivel de bitstream, algo que un remuxer deliberadamente no
  hace), y no está verificado si la entrada de la capa de mejora del disco
  puede aparecer como una pista de vídeo extra espuria.

## Fixtures de disco

Las pruebas de regresión de los analizadores (`tests/test_parser_fixtures.py`)
procesan archivos `.mpls` / `.clpi` / `.ifo` reales capturados de discos
concretos. Esos blobs **no se suben** al repositorio (para evitar redistribuir
metadatos de disco), por lo que las pruebas se omiten en un clon nuevo.

Para ejecutarlas localmente, captura los fixtures en `tests/fixtures/` tú mismo:

```sh
# Blu-ray, desde un .iso (los números de playlist/clip dependen del disco):
uv run python scripts/iso_extract.py disc.iso tests/fixtures "BDMV/PLAYLIST/00800.mpls" "BDMV/CLIPINF/00875.clpi" "BDMV/META/DL/bdmt_eng.xml"

# DVD, desde una carpeta VIDEO_TS extraída:
cp VIDEO_TS/VIDEO_TS.IFO tests/fixtures/dvd_video_ts.ifo
cp VIDEO_TS/VTS_01_0.IFO tests/fixtures/dvd_vts_01_0.ifo
```

`scripts/inspect_fixtures.py` vuelve a procesar lo que haya en
`tests/fixtures/` e imprime los valores que esperan las pruebas, lo que resulta
útil al cambiar a un disco nuevo.

## Vibe check

Este proyecto fue *vibe coded* — descrito en su mayor parte a un LLM e iterado,
en lugar de tecleado línea a línea. El análisis de formatos de disco y las
decisiones de comportamiento son deliberadas y están cubiertas por
pruebas contra imágenes de disco reales; el resto puede haberse escrito con una
confianza inmerecida.

## Licencia

GPL-3.0-or-later. Consulta [LICENSE](LICENSE).
