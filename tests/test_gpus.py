"""Parsing of nvidia-smi queries."""

from __future__ import annotations

from trainhub.core.gpus import parse_gpus, query_apps, query_gpus, visible_indices

GPUS = """\
0, GPU-aaa, NVIDIA A100-SXM4-80GB, 40123, 81920, 97, 61
1, GPU-bbb, NVIDIA A100-SXM4-80GB, 4, 81920, 0, 33
2, GPU-ccc, Tesla, V100, [N/A], [N/A], [Not Supported], 40
"""
APPS = """\
GPU-aaa, 1234, 30000
GPU-aaa, 1240, 10000
GPU-ccc, 99, [N/A]
"""


def test_queries_are_csv_without_units() -> None:
    assert query_gpus()[0] == "nvidia-smi" and "--format=csv,noheader,nounits" in query_gpus()
    assert query_apps()[1].startswith("--query-compute-apps=gpu_uuid,")


def test_parse_gpus() -> None:
    g = parse_gpus(GPUS, APPS)
    assert [x.index for x in g] == [0, 1, 2]
    assert g[0].memory_used_mib == 40123 and g[0].utilization_pct == 97 and g[0].processes == 2
    assert g[1].processes == 0 and g[1].temperature_c == 33
    # comma in the name, unsupported values
    assert g[2].name == "Tesla, V100" and g[2].memory_used_mib is None
    assert g[2].utilization_pct is None and g[2].processes == 1
    assert parse_gpus("") == [] and parse_gpus("garbage\n") == []


def test_visible_indices() -> None:
    assert visible_indices(None) is None and visible_indices("all") is None
    assert visible_indices("0, 2") == [0, 2]
    assert visible_indices("") == [] and visible_indices("none") == []
