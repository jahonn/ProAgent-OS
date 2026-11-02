"""The checked-in mock demo recording stays a valid, offline, secret-free asset.

Does not re-run the demos. Regenerating is `python scripts/record_demo.py`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_NS = {"svg": "http://www.w3.org/2000/svg"}


def _load():
    name = "record_demo"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "record_demo.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_soft_wrap_keeps_words_inside_the_terminal():
    rec = _load()
    assert rec.soft_wrap("", 100) == [""]
    assert rec.soft_wrap("short line", 100) == ["short line"]
    sentence = (
        "Key points were retrieved from the knowledge base and tools. "
        "A recommendation has been proposed where appropriate."
    )
    wrapped = rec.soft_wrap(sentence, 80)
    assert len(wrapped) >= 2
    assert all(len(row) <= 80 for row in wrapped)
    assert " ".join(row.strip() for row in wrapped) == sentence

    indented = rec.soft_wrap("    " + ("alpha " * 30).strip(), 40)
    assert all(len(row) <= 40 for row in indented)
    assert all(row.startswith("    ") for row in indented)
    assert " ".join(row.strip() for row in indented) == ("alpha " * 30).strip()

    hard = rec.soft_wrap("x" * 50, 20)
    assert all(len(row) <= 20 for row in hard)
    assert "".join(row.strip() for row in hard) == "x" * 50


def test_builder_emits_a_cast_and_a_smil_svg_without_running_demos():
    rec = _load()
    lines = [
        "$ export PROAGENT_LLM_PROVIDER=mock",
        "$ python scripts/demo.py",
        "=== ProAgent OS Demo ===",
        "status=completed",
        "x" * 120,
    ]
    visual = rec.present(lines)
    assert visual[0].appear_ms == 0
    assert all(len(row.text) <= rec.COLS for row in visual)
    assert visual[-1].appear_ms >= visual[0].appear_ms

    cast = rec.build_cast(visual)
    header, *events = cast.splitlines()
    parsed = json.loads(header)
    assert parsed["version"] == 2
    assert parsed["width"] == rec.COLS
    assert parsed["height"] == rec.ROWS
    times = []
    for raw in events:
        event = json.loads(raw)
        assert event[1] == "o"
        times.append(event[0])
    assert times == sorted(times)

    svg = rec.build_svg(visual, visual[-1].appear_ms + rec.HOLD_MS)
    root = ET.fromstring(svg)
    assert root.tag == f"{{{SVG_NS['svg']}}}svg"
    anims = root.findall(".//svg:animate", SVG_NS) + root.findall(".//svg:animateTransform", SVG_NS)
    assert anims
    for anim in anims:
        assert anim.get("repeatCount") == "indefinite"
        stamps = [float(part) for part in anim.get("keyTimes", "").split(";") if part != ""]
        assert stamps[0] == 0
        assert stamps[-1] == 1
        assert all(later >= earlier for earlier, later in zip(stamps, stamps[1:]))
        assert len(anim.get("values", "").split(";")) == len(stamps)
    lowered = svg.lower()
    assert "<script" not in lowered
    assert "<style" not in lowered
    assert "xlink:href" not in lowered


def test_checked_in_recording_matches_the_mock_path():
    rec = _load()
    cast_path = ROOT / "docs" / "demo.cast"
    svg_path = ROOT / "docs" / "demo.svg"
    cast = cast_path.read_text(encoding="utf-8")
    svg = svg_path.read_text(encoding="utf-8")
    assert cast_path.stat().st_size < 80_000
    assert svg_path.stat().st_size < 180_000

    header, *event_lines = [line for line in cast.splitlines() if line.strip()]
    parsed = json.loads(header)
    assert parsed["version"] == 2
    assert parsed["width"] == rec.COLS
    assert parsed["height"] == rec.ROWS
    assert parsed["title"] == "ProAgent OS mock demo"
    assert 8 < parsed["duration"] < 45

    times = []
    chunks = []
    for raw in event_lines:
        event = json.loads(raw)
        assert event[1] == "o"
        times.append(event[0])
        chunks.append(event[2])
    assert times == sorted(times)
    visible = rec.visible_text("".join(chunks))
    for marker in rec.REQUIRED_MARKERS:
        assert marker in visible
    assert "python scripts/demo.py" in visible
    assert "python scripts/demo_support_desk.py" in visible
    assert "python scripts/demo_ops_incident.py" in visible
    assert "PROAGENT_LLM_PROVIDER=mock" in visible
    plain_lines = []
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(chunks))
    for line in plain.splitlines():
        assert len(line) <= rec.COLS
        plain_lines.append(line)
    assert len(plain_lines) > rec.ROWS

    root = ET.fromstring(svg)
    assert root.get("width") == str(18 * 2 + round(rec.COLS * rec.CHAR_PX))
    assert root.get("height") == str(44 + rec.ROWS * rec.LINE_PX + 14)
    assert "repeatCount" in svg
    for marker in (
        "=== ProAgent OS Demo ===",
        "pk_demo_acme_analytics",
        "=== ProAgent OS Support Desk Demo ===",
        "=== ProAgent OS Ops Incident Demo ===",
        "pk_demo_harborline_oncall",
    ):
        assert marker in svg
    for marker in rec.REQUIRED_MARKERS:
        assert marker in rec.visible_text(svg)
    lowered = svg.lower()
    assert "<script" not in lowered
    assert "<style" not in lowered
    assert "xlink:href" not in lowered
    blob = cast + svg
    for forbidden in ("sk-", "PROAGENT_LLM_API_KEY", "/tmp/", "api.openai.com", ".env"):
        assert forbidden not in blob
