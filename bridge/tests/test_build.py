"""Unit tests for the pure half of `resolve_cli build` (no Resolve needed).

Run:  py -3 -m pytest engine/resolve_bridge/tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import resolve_cli as cli  # noqa: E402


def _spec(**over):
    spec = {
        "schema_version": "1.0.0", "format": "short-clip", "rate": 30,
        "resolution": [1080, 1920], "timeline": "demo-01",
        "tracks": {
            "V1": [
                {"id": "b1", "src": "D:/x/b1.mp4", "in": 0, "out": 90, "record": 0, "grade": "cinematic-10"},
                {"id": "a1", "src": "D:/x/a1.mp4", "in": 12, "out": 102, "record": 90, "fill": 1.05},
            ],
            "A1": [{"id": "vo", "src": "D:/x/vo.wav", "in": 0, "out": 900, "record": 0}],
            "V2": [{"id": "t1", "overlay": "title", "template": "name-card", "text": "Title", "record": 30, "dur": 60}],
        },
    }
    spec.update(over)
    return spec


def test_validate_ok_sorts_media_and_separates_overlays():
    media, overlays, problems = cli.validate_spec(_spec())
    assert problems == []
    assert [(r, c["id"]) for r, _, _, c in media] == [("A1", "vo"), ("V1", "b1"), ("V1", "a1")]
    assert [c["id"] for _, c in overlays] == ["t1"]


def test_validate_rejects_float_frames_and_bad_out():
    s = _spec()
    s["tracks"]["V1"][0]["in"] = 1.5
    s["tracks"]["V1"][1]["out"] = 12
    _, _, problems = cli.validate_spec(s)
    assert any("must be integer frames" in p for p in problems)
    assert any("out (12) must be > in (12)" in p for p in problems)


def test_validate_rejects_overlap_duplicate_id_and_bad_role():
    s = _spec()
    s["tracks"]["V1"][1]["record"] = 80          # b1 ends at 90 -> overlap
    s["tracks"]["A1"][0]["id"] = "b1"            # duplicate id
    s["tracks"]["X1"] = []                       # bad role
    _, _, problems = cli.validate_spec(s)
    assert any("overlaps" in p for p in problems)
    assert any("duplicate clip id" in p for p in problems)
    assert any("not V<n>/A<n>" in p for p in problems)


def test_validate_requires_rate_resolution_timeline():
    _, _, problems = cli.validate_spec({"schema_version": "1.0.0", "tracks": {"V1": []}})
    joined = " ".join(problems)
    assert "rate must be" in joined and "resolution must be" in joined and "timeline" in joined


def test_clip_info_geometry_exclusive_out_and_offset():
    clip = {"id": "c", "src": "x", "in": 12, "out": 102, "record": 90}
    info = cli.clip_info(clip, "V", 1, "ITEM", 108000)
    assert info == {"mediaPoolItem": "ITEM", "startFrame": 12, "endFrame": 101,
                    "recordFrame": 108090, "trackIndex": 1, "mediaType": 1}
    assert cli.clip_info(clip, "A", 2, "ITEM", 0)["mediaType"] == 2


def test_verify_placement_reports_mismatch_only():
    clip = {"id": "c", "in": 12, "out": 102, "record": 90}
    assert cli.verify_placement(clip, 108090, 90, 108000) is None
    msg = cli.verify_placement(clip, 108090, 89, 108000)
    assert msg and "spec wanted 108090 for 90" in msg


def test_parse_lut_map_layers_and_rejects_malformed():
    m = cli.parse_lut_map(["warm=Looks/warm.cube"])
    assert m == {**cli.DEFAULT_LUT_MAP, "warm": "Looks/warm.cube"}
    with pytest.raises(ValueError):
        cli.parse_lut_map(["nopath"])


def test_norm_path_is_case_and_slash_insensitive():
    assert cli._norm_path("D:/Footage/a.MP4") == cli._norm_path(r"d:\footage\a.MP4")
