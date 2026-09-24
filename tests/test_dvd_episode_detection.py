"""Tests for DVD episode-PGC detection helpers."""

from __future__ import annotations

from typing import Any

from dvdifo import (
    _EnumeratedPgc,
    _find_play_all_pgc,
    _has_distinct_pgc_cell_signatures,
    _candidate_episode_clusters,
)


def test_candidate_clusters_prefer_largest_then_shorter_tie() -> None:
    pgcs: list[_EnumeratedPgc] = [
        (1, 10, 1000.0, 10),
        (2, 20, 1010.0, 10),
        (3, 30, 990.0, 10),
        (4, 40, 1400.0, 10),
        (5, 50, 1410.0, 10),
    ]

    clusters = _candidate_episode_clusters(pgcs)

    # Largest first; between equal-size clusters, the shorter centre wins
    # (episode-length PGCs over long extras).
    assert [[pgc[0] for pgc in cluster] for cluster in clusters[:2]] == [
        [1, 2, 3],
        [4, 5],
    ]


def test_candidate_clusters_keep_longer_rival_available() -> None:
    """Superman (1988): seven ~19-minute episodes tie against seven ~5-minute
    shorts. The shorter cluster is preferred first, but the episodes cluster
    stays available for the guard chain to select."""
    pgcs: list[_EnumeratedPgc] = [
        (7, 70, 1146.0, 4),
        (9, 90, 1140.0, 4),
        (11, 110, 1143.0, 4),
        (8, 80, 286.0, 3),
        (10, 100, 285.0, 3),
        (12, 120, 287.0, 3),
    ]

    clusters = _candidate_episode_clusters(pgcs)

    assert [[pgc[0] for pgc in cluster] for cluster in clusters[:2]] == [
        [8, 10, 12],  # shorter centre preferred first
        [7, 9, 11],  # episodes remain the next candidate
    ]


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


def test_cluster_is_dwarfed_by_long_feature() -> None:
    """The dwarfing guards, pinned to the two real discs that motivated
    them (see the fixture-based tests in test_parser_fixtures.py):
    a movie beside short extras dwarfs their cluster, while a play-all
    compilation — which also runs several times any single episode — is
    exempt because it plays the episodes' own cells."""
    from dvdifo import _cluster_is_dwarfed

    # Synthetic shape only: the cell-level compilation exemption needs real
    # IFO cell tables, covered by the Treasure Planet (dwarfed by an 869s
    # featurette) and Tex Avery (7114s compilation exempt) fixture tests.
    cluster: list[_EnumeratedPgc] = [
        (1, 10, 1200.0, 10),
        (2, 20, 1210.0, 10),
        (3, 30, 1190.0, 10),
    ]
    dwarfed = cluster + [(4, 40, 4200.0, 20)]

    assert _cluster_is_dwarfed(b"", dwarfed, cluster, None) is True
    # The play-all chain (matched by duration sum) is excluded outright.
    assert _cluster_is_dwarfed(b"", dwarfed, cluster, 4) is False
    # A chain below the 3x threshold never qualifies as a dwarf suspect.
    assert (
        _cluster_is_dwarfed(b"", cluster + [(4, 40, 2400.0, 20)], cluster, None)
        is False
    )
