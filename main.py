#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich",
# ]
# ///
"""
MakeMKV-like DVD/Blu-ray ripper using mkvmerge (MKVToolNix)

Copyright (C) 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

---

Contains portions inspired by:
- dvdutils (MIT) https://pypi.org/project/dvdutils/
- pyparsedvd (MIT) https://github.com/Ichunjo/pyparsedvd
- pyparsebluray (MIT) https://github.com/Ichunjo/pyparsebluray (STN table parsing,
  CHARACTER_CODE, HEVC HDR metadata)
- bluinfo (GPL-3.0) https://github.com/SavSanta/bluinfo (CLPI format layout,
  stream attribute constants)
- libbluray (GPL-2.0) https://code.videolan.org/videolan/libbluray (BD-ROM
  structure reference, CLPI/MPLS parsing implementation)
- libbluray documentation https://videolan.videolan.me/libbluray/index.html
  (BD-J, UDF structure, disc metadata)
- ace20022/libbluray (GPL-2.0) https://github.com/ace20022/libbluray
  (clean Pythonic CLPI/MPLS parsing reference)
- Blu-ray Disc Read-Only Format specifications (BD-ROM)
"""

# Licensed under GPL-3.0-or-later

from __future__ import annotations

# Everything except this entry point has been extracted into per-concern
# modules: models, i18n, settings, dvdifo, vobsub, disc_reader, probe,
# bluray, dvdbuild, scan, mkv, and cli (which owns the real main()).
from cli import main

if __name__ == "__main__":
    main()
