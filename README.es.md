# mkvsmith

> [English](README.md) · [Español](README.es.md)

Convierte tus DVD, Blu-ray y HD DVD en archivos MKV, con todo su audio,
subtítulos y capítulos.

mkvsmith lee la propia estructura del disco para saber qué contiene: qué
título es la película, cuáles son episodios, cuáles son extras y cuáles son
menús o relleno. Después deja la copia en sí a
[mkvmerge](https://mkvtoolnix.download/). Escanear un disco lleva segundos,
y mkvmerge es el único otro programa que necesitas.

## Qué hace

- **Extrae DVD, Blu-ray y HD DVD**: desde una unidad, una imagen ISO o una
  carpeta de disco (`VIDEO_TS`, `BDMV`, `HVDVD_TS`).
- **Encuentra la película principal por ti**, o todos los episodios de un
  disco de serie, y oculta menús, tráileres y títulos señuelo.
- **Conserva lo importante**: las pistas de audio que quieras, los
  subtítulos (incluidos los subtítulos de DVD que otras herramientas pasan
  por alto), los capítulos y, si quieres, los subtítulos ocultos (CC).
- **Une los distintos montajes de una película** (cines, extendido, …) en un
  solo MKV cuyas ediciones puedes cambiar en tu reproductor.
- **Extras opcionales**: datos de la película y carátula de
  [TMDB](https://www.themoviedb.org/), y nombres del disco y de los episodios
  de [TheDiscDB](https://thediscdb.com/).

## Primeros pasos

Necesitas:

1. **Python 3.12 o posterior**.
2. **MKVToolNix**, por `mkvmerge`: `sudo apt install mkvtoolnix` en
   Debian/Ubuntu, `brew install mkvtoolnix` en macOS, o el instalador de
   [mkvtoolnix.download](https://mkvtoolnix.download/).
3. **[uv](https://docs.astral.sh/uv/)**, que instala todo lo demás por ti.

Después:

```sh
git clone https://github.com/Zorro92/mkvsmith
cd mkvsmith
uv run ./main.py
```

Eso abre mkvsmith. La primera vez hace unas preguntas (tus idiomas, si
quieres conservar los subtítulos, etc.). Pulsa Intro para quedarte con cada
sugerencia. Después, elige un disco y extráelo.

> **Discos cifrados:** como con cualquier extractor, leer un disco comprado
> necesita `libdvdcss` (DVD) o `libaacs` (Blu-ray) instalados en tu sistema.
> mkvsmith no descifra nada por sí mismo, y te avisa cuando un disco sigue
> cifrado.

## Cómo se usa

Todo es una lista. Muévete con las flechas o la rueda del ratón, y elige
una línea con Intro o con un clic. **Esc** vuelve atrás y **q** sale. La
barra de abajo muestra las teclas de cada pantalla, y también puedes hacer
clic en ellas.

1. **Elige un disco.** Escoge una unidad, o busca una ISO o una carpeta de
   disco. También puedes abrir uno directamente:
   `uv run ./main.py pelicula.iso`.
2. **Elige qué extraer.** Arriba de la lista está la opción más probable: la
   película principal, o todos los episodios en un disco de serie. Debajo
   están todos los títulos. **Espacio** marca varios para extraerlos de una
   vez, e Intro sobre un título muestra sus pistas de audio y subtítulos,
   para que elijas cuáles conservar.
3. **Extrae.** La primera vez, mkvsmith pregunta dónde guardar; después
   muestra el progreso de cada archivo. Esc pregunta antes de detener, y
   luego borra el archivo sin terminar.

Tus elecciones se guardan, y puedes cambiarlas cuando quieras en
**Ajustes**, en el menú principal.

## Extras opcionales

### Datos y carátula de la película (TMDB)

mkvsmith puede etiquetar cada extracción con el título, el año, el reparto,
el argumento y la carátula de la película, sacados de TMDB. Necesitas una
[clave de API de TMDB](https://www.themoviedb.org/settings/api) gratuita.
Escríbela en **Ajustes → tmdb.api_key** y pon **tmdb.tagging** en `ask`
(pregunta en cada disco) o `always`.

### Nombres del disco (TheDiscDB)

[TheDiscDB](https://thediscdb.com/) es una base de datos de discos hecha por
la comunidad. Con **discdb.enabled** activado, mkvsmith busca tu disco y usa
sus títulos y nombres de episodio. Ayuda sobre todo en discos que esconden
la película real entre docenas de títulos falsos. Solo se envían los números
de identificación del disco, nunca su contenido. mkvsmith también puede
preparar los archivos para [aportar un disco](https://thediscdb.com/) que
hayas escaneado (`--discdb-contribute`).

## La línea de comandos

Para scripts, o si simplemente prefieres escribir, mkvsmith también funciona
sin su interfaz. Cualquier opción de acción va directa al trabajo:

```sh
uv run ./main.py pelicula.iso -m           # extrae la película principal (o todos los episodios)
uv run ./main.py pelicula.iso -t 1,3       # extrae los títulos 1 y 3
uv run ./main.py pelicula.iso -a ~/rips    # extrae todos los títulos en ~/rips
uv run ./main.py pelicula.iso -i           # solo lista los títulos
```

`--help` muestra las opciones de uso diario y `--help-all`, todas. Las
opciones de la línea de comandos sustituyen a tus ajustes guardados solo en
esa ejecución. La [referencia](docs/REFERENCE.md) (en inglés) tiene los
detalles.

## Conviene saber

La [referencia](docs/REFERENCE.md) (en inglés) trata todo esto con más
detalle.

- **Dónde están los ajustes:** `~/.config/mkvsmith/config.json`. Define
  `MKVSMITH_CONFIG` para usar otro archivo.
- **Plataformas:** hecho y probado en Linux. Los discos desde ISOs y carpetas
  deberían funcionar en cualquier sistema. Leer directamente de una unidad
  funciona en Linux, y en Windows se reconocen las letras de unidad, pero
  Windows y macOS están en gran parte sin probar.
- **Imágenes de discos grabados** (regrabables o grabables) aún no se pueden
  abrir. Copia antes el disco a una carpeta.
- **Cambiar de edición** en un MKV multiedición necesita un reproductor
  compatible con capítulos ordenados, como mpv o VLC.
- **Los nombres de episodio** son como `Serie - S01D02 - Episode 3` cuando el
  nombre del disco o de la carpeta lleva la temporada y el número de disco;
  si no, `<nombre del disco> - Episode 3`. Un disco no puede saber cuántos
  episodios había en los anteriores, así que la numeración empieza de nuevo
  en cada disco.
- **Blu-ray de series largas** que reproducen todo el disco como un único
  vídeo de 15–20 horas (p. ej. Sgt. Frog) se pueden dividir en un archivo
  por episodio. mkvsmith lo ofrece cuando detecta uno. Algunos reproductores
  por hardware muestran la pantalla en negro en un episodio dividido que
  empieza a mitad de un clip; mkvsmith avisa cuando pasa, y la reproducción
  por software funciona bien.
- **HDR:** HDR10 y HDR10+ pasan sin cambios. Dolby Vision no se ha probado;
  solo se conserva la capa base HDR10.
- **Los archivos temporales** van a `/var/tmp`. Las extracciones grandes
  evitan llenar un disco en RAM. Cambia la carpeta con `temp.dir` si andas
  justo de espacio.

## Contribuir

Los informes de errores y los pull requests son bienvenidos. Consulta
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (en inglés) para ejecutar las
comprobaciones y las pruebas, incluidos los fixtures opcionales sacados de
discos.

## Vibe check

Este proyecto se hizo con *vibe coding*: casi todo se describió a un LLM y se
fue iterando, en lugar de escribirlo línea a línea. El análisis de los
formatos de disco y las decisiones de comportamiento son deliberados y están
cubiertos por pruebas con imágenes de discos reales; el resto puede haberse
escrito con una confianza injustificada.

## Licencia

GPL-3.0-or-later. Consulta [LICENSE](LICENSE).
