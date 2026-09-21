"""The 21.1 command catalog: registry consistency, argparse and sidecar views.

What is asserted is the contract the three consumers rely on -- every spec'd
write command is in resolve_cli.WRITE_COMMANDS (the AST-parsed literal
check_readonly.py enforces), every spec builds a working argparse subparser,
and the sidecar coerces each field by the command's own declared kind.
"""

import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ops_common  # noqa: E402
import resolve_cli  # noqa: E402
import serve_http  # noqa: E402


def test_every_spec_is_a_command_and_writes_match_the_literal():
    for spec in ops_common.SPECS:
        assert resolve_cli.COMMANDS[spec["name"]] is spec["fn"]
        listed = spec["name"] in resolve_cli.WRITE_COMMANDS
        assert listed == spec["writes"], (
            "%s: writes=%s but WRITE_COMMANDS %s it" % (spec["name"], spec["writes"],
                                                        "lists" if listed else "omits"))
        if listed:
            assert resolve_cli.WRITE_COMMANDS[spec["name"]] == spec["fn"].__name__
    # and nothing in the literal points at a function that no longer exists
    spec_names = {s["name"]: s for s in ops_common.SPECS}
    for name, fn_name in resolve_cli.WRITE_COMMANDS.items():
        if name in spec_names:
            assert spec_names[name]["fn"].__name__ == fn_name
        else:
            assert name in resolve_cli.LEGACY_COMMANDS, "%s is neither legacy nor spec'd" % name


def test_catalog_is_large_and_unique():
    names = [s["name"] for s in ops_common.SPECS]
    assert len(names) == len(set(names))
    assert len(names) >= 120


def test_write_handlers_guard_first():
    """A write handler must refuse without --yes before touching Resolve.
    Driven with a namespace that has every default and yes=False; the
    handler must exit with the guard message and never call the resolve
    object (a Mock that raises on any attribute access)."""
    class Explode:
        def __getattr__(self, name):
            raise AssertionError("Resolve touched before the --yes guard: %s" % name)

    for spec in ops_common.SPECS:
        if not spec["writes"]:
            continue
        ns = argparse.Namespace(json=True, yes=False, timeout=25, **ops_common.spec_defaults(spec))
        # positional required args are None here; `need()` may fire before the
        # guard for commands whose guard message names them -- both outcomes
        # exit through SystemExit without touching Resolve, which is the point.
        with pytest.raises(SystemExit):
            spec["fn"](ns, Explode())


def test_argparse_builds_every_spec():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    for spec in ops_common.SPECS:
        resolve_cli._add_spec_parser(sub, spec)
    args = parser.parse_args(["add-marker", "--frame", "12", "--color", "Red", "--custom-data", '{"by":"t"}'])
    assert args.frame == 12 and args.color == "Red" and args.custom_data == '{"by":"t"}'
    assert args.name is None            # per-command default, not the legacy "pp_render"
    assert args.duration == 1
    args = parser.parse_args(["delete-clips", "a", "b", "--bin", "A/B"])
    assert args.clips == ["a", "b"] and args.bin == "A/B"
    args = parser.parse_args(["set-track", "V1", "--enable", "false"])
    assert ops_common.tri(args, "enable") is False
    args = parser.parse_args(["keyframe-mode"])
    assert args.command == "keyframe-mode"


def test_sidecar_coerces_by_spec_kind():
    args = serve_http._build_args("add-marker", {"frame": "12", "custom-data": {"by": "t"}, "yes": True})
    assert args.frame == 12 and args.custom_data == {"by": "t"} and args.yes is True
    args = serve_http._build_args("add-marker", {"frame": 3, "custom-data": '{"k": 1}'})
    assert args.custom_data == {"k": 1}
    args = serve_http._build_args("set-track", {"track": "V1", "enable": True})
    assert args.enable == "true" and ops_common.tri(args, "enable") is True
    args = serve_http._build_args("delete-clips", {"clips": "one"})
    assert args.clips == ["one"]
    args = serve_http._build_args("apply-cdl", {"items": ["V1:1"], "saturation": "0.8"})
    assert args.saturation == 0.8


def test_sidecar_rejects_unknown_and_bad_fields():
    with pytest.raises(ValueError, match="not an argument"):
        serve_http._build_args("add-marker", {"bogus": 1})
    with pytest.raises(ValueError, match="integer"):
        serve_http._build_args("add-marker", {"frame": "twelve"})
    with pytest.raises(ValueError, match="@file"):
        serve_http._build_args("set-project-settings", {"settings": "@C:/x.json"})


def test_sidecar_defaults_are_per_command():
    """`name` defaults to None on add-marker even though the legacy render
    command's stem used to live in a shared default table."""
    assert serve_http._build_args("add-marker", {}).name is None
    assert serve_http._build_args("render", {}).name is None
    assert serve_http._build_args("render", {}).out is None


def test_catalog_exposes_arg_contract():
    cat = serve_http._catalog()
    by_name = {c["name"]: c for c in cat["commands"]}
    assert by_name["add-marker"]["method"] == "POST"
    assert any(a["name"] == "custom-data" and a["kind"] == "json" for a in by_name["add-marker"]["args"])
    assert by_name["timeline-markers"]["method"] == "GET"
    assert cat["count"] == len(by_name)


def test_jsonval_accepts_text_and_structured():
    assert ops_common.jsonval('{"a": 1}', "x") == {"a": 1}
    assert ops_common.jsonval({"a": 1}, "x") == {"a": 1}
    assert ops_common.jsonval(None, "x") is None
    with pytest.raises(SystemExit):
        ops_common.jsonval("{not json", "x")


def test_markers_rows_decodes_custom_data():
    rows = ops_common.markers_rows({"96": {"color": "Blue", "customData": '{"by":"bridge"}'},
                                    "12": {"color": "Red", "customData": "plain"}})
    assert [r["frame"] for r in rows] == [12, 96]
    assert rows[1]["customData"] == {"by": "bridge"}
    assert rows[0]["customData"] == "plain"


def test_parse_track():
    assert ops_common.parse_track("V2") == ("video", 2)
    assert ops_common.parse_track("audio:1") == ("audio", 1)
    assert ops_common.parse_track("s3") == ("subtitle", 3)
    with pytest.raises(SystemExit):
        ops_common.parse_track("X1")
