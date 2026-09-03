# mkvsmith

> [English](README.md) · [Español](README.es.md)

Extractor de DVD/Blu-ray al estilo MakeMKV que produce archivos MKV usando
[mkvmerge](https://mkvtoolnix.download/) (MKVToolNix).

`mkvsmith` lee las estructuras del disco directamente — metadatos
`.mpls` / `.clpi` / `.ifo` / BDMV — en lugar de sondear los flujos de medios.
Eso hace que el escaneo sea rápido y con pocas dependencias: la única
herramienta de medios externa que necesita es `mkvmerge`. Imita deliberadamente
el comportamiento de MakeMKV cuando ese comportamiento es el valor por defecto
más sensato, pero es una reimplementación independiente con licencia GPL.

## Características

- **Extrae discos DVD (VIDEO_TS) y Blu-ray (BDMV), ISOs, archivos
  `.m2ts`/`.vob` sueltos y archivos de vídeo normales** a Matroska (`.mkv`).
- **Conserva el audio, los subtítulos y los capítulos**, incluidos los flujos
  de subimagen de DVD que otros escáneres más simples pasan por alto.
- **Etiquetado TMDB opcional** — metadatos y carátulas incrustados directamente
  en el mux.

## Requisitos

- **Python 3.12+**
- **mkvmerge** (MKVToolNix) — la única herramienta de medios externa y un
  requisito imprescindible para el multiplexado.
- **7z** (`p7zip-full` en Debian/Ubuntu) — para leer imágenes ISO.
- **sudo + mount** — *opcional*, solo para montar ISOs en bucle.
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

# extrae todos los episodios de TV detectados
uv run ./main.py /ruta/al/disco -e

# escribe la salida en un directorio concreto (segundo argumento posicional)
uv run ./main.py /ruta/al/disco -m ~/rips
```

### Modo interactivo

Ejecuta sin `-t/-m/-a/-e` para entrar en el prompt interactivo:

```text
mkvsmith> n          # muestra los detalles del título n
mkvsmith> r 1        # extrae el título 1
mkvsmith> rm         # extrae la película principal
mkvsmith> re         # extrae todos los episodios
mkvsmith> ra         # extrae todos los títulos
mkvsmith> q          # salir
```

### Opciones comunes

| Opción | Descripción |
|---|---|
| `-t, --title N` | Extrae un título concreto |
| `-m, --main` | Extrae la película principal detectada |
| `-a, --all` | Extrae todos los títulos |
| `-e, --episodes` | Extrae todos los episodios de TV detectados |
| `-i, --info` | Solo escanea y lista los títulos |
| `-s, --streams` | Selecciona pistas (p. ej. `v:0 a:eng s:all`) |
| `-l, --lang` | Idiomas preferidos (por defecto `eng,en,und`) |
| `--all-audio` / `--no-all-audio` | Conserva todo el audio (activado por defecto) |
| `--no-subs` | Descarta los subtítulos |
| `--no-forced` | Descarta los subtítulos forzados |
| `--min-duration N` | Ignora los títulos de menos de N segundos |
| `--show-all` | Muestra los títulos de baja calidad (menús/tráileres) |
| `--temp-dir DIR` | Directorio temporal (usa una ruta en disco para ISOs grandes) |
| `--ram-limit FRAC` | Fracción máxima de RAM para directorios temporales en RAM |
| `--no-sudo` | Omite el montaje en bucle con sudo |
| `--tag` / `--no-tag` | Controles de etiquetado TMDB |
| `--ui-lang LANG` | Idioma de la interfaz (p. ej. `en`, `es`) |
| `--debug` | Registro de depuración detallado |

## Notas

- Los discos comerciales cifrados necesitan `libdvdcss` (DVD) / `libaacs`
  (Blu-ray) a nivel de sistema.
- El directorio temporal por defecto suele ser un tmpfs respaldado en RAM en
  Linux. `mkvsmith` lo detecta y vuelca de forma transparente las extracciones
  demasiado grandes a disco (`--ram-limit` controla el umbral).
- El montaje directo de ISO en bucle usa `sudo`; pasa `--no-sudo` para
  desactivarlo.
- **La salida MKV multi-edición es experimental.** Está desactivada por defecto
  y oculta tras `--debug` (que expone `--multi-edition` y el comando interactivo
  `me`). La reproducción a través de los puntos donde se unen las ediciones
  puede no funcionar en todos los reproductores.
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
# Blu-ray, desde un .iso mediante 7z (los números de playlist/clip dependen del disco):
7z e disc.iso "BDMV/PLAYLIST/00800.mpls" "BDMV/CLIPINF/00875.clpi" "BDMV/META/DL/bdmt_eng.xml" -otests/fixtures -y

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
decisiones de comportamiento de MakeMKV son deliberadas y están cubiertas por
pruebas contra imágenes de disco reales; el resto puede haberse escrito con una
confianza inmerecida.

## Licencia

GPL-3.0-or-later. Consulta [LICENSE](LICENSE).

`mkvsmith` no está afiliado con MakeMKV ni respaldado por él.
