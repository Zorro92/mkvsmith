"""Tests for DVD episode-PGC detection helpers."""

from __future__ import annotations

from typing import Any

from dvdifo import (
    _EnumeratedPgc,
    _find_play_all_pgc,
    _has_distinct_pgc_cell_signatures,
    _largest_duration_cluster,
)


def test_largest_duration_cluster_prefers_episode_cluster_and_shorter_tie() -> None:
    pgcs: list[_EnumeratedPgc] = [
        (1, 10, 1000.0, 10),
        (2, 20, 1010.0, 10),
        (3, 30, 990.0, 10),
        (4, 40, 1400.0, 10),
        (5, 50, 1410.0, 10),
    ]

    cluster = _largest_duration_cluster(pgcs)

    assert [pgc[0] for pgc in cluster] == [1, 2, 3]


def test_find_play_all_pgc_matches_episode_duration_sum() -> None:
    episodes: list[_EnumeratedPgc] = [
        (1, 10, 1200.0, 10),
        (2, 20, 1210.0, 10),
    ]
    all_pgcs: list[_EnumeratedPgc] = [
        *episodes,
        (3, 30, 100.0, 1),
        (4, 40, 2400.0, 20),
        (5, 50, 2600.0, 20),
    ]

    assert _find_play_all_pgc(all_pgcs, episodes, 60.0) == 4


def test_cell_signature_check_rejects_duplicate_verified_tables(
    monkeypatch: Any,
) -> None:
    pgcs: list[_EnumeratedPgc] = [
        (1, 10, 100.0, 2),
        (2, 20, 100.0, 2),
        (3, 30, 100.0, 2),
    ]
    signatures = iter([((1, 1), (1, 2)), ((1, 1), (1, 2)), None])
    monkeypatch.setattr(
        "dvdifo._pgc_cell_position_signature",
        lambda _data, _pgc_abs, _cells: next(signatures),
    )

    assert _has_distinct_pgc_cell_signatures(b"ifo", pgcs) is False
