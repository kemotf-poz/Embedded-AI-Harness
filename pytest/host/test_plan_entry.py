"""Host tier for the build skill's plan-entry helper
(.claude/skills/build/scripts/plan_entry.py). Fixture plans live in pytest's
own tmp_path; nothing outside it is read or written."""
import difflib
import importlib.util
import pathlib
import sys

import pytest
import yaml

HELPER = (pathlib.Path(__file__).resolve().parents[2]
          / ".claude/skills/build/scripts/plan_entry.py")
_spec = importlib.util.spec_from_file_location("plan_entry", HELPER)
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)

PLAN = """\
# Test plan — fixture
capabilities:
  wifi-ap-outage:
    what:      Stopping and restarting the access point on demand
    available: no        # cannot be done

tests:
  - id: JRN-01.1
    kind: standard
    verifies: [REQ-001]
    status: not done
    reason: test not written
    impl:

  # the second test, with a comment between entries
  - id: TC-MQTT-01
    kind: standard
    status: "failed"
    reason: >-
      broker refused the connection
    commit: 1a2b3c4

  - id: TC-MQTT-02
    status: successful
"""

# Same content shape, but under another key, at another indent, and with the
# first field of an entry not being `id`.
OTHER_LAYOUT = """\
suite:
    cases:
        -   name: first
            id: A-1
            status: not done
        -   id: A-2
            status: successful
"""


def run(tmp_path, text, *argv):
    plan = tmp_path / "test-plan.yaml"
    plan.write_text(text)
    code = pe.main(["--plan", str(plan), *argv])
    return code, plan.read_text()


def entry(text, test_id):
    data = yaml.safe_load(text)
    found = [e for e in _walk(data) if e.get("id") == test_id]
    assert len(found) == 1
    return found[0]


def _walk(node):
    if isinstance(node, dict):
        if "id" in node:
            yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def changed_lines(before, after):
    """Lines removed or added, as text — what a reviewer would see in a diff."""
    return [ln for ln in difflib.unified_diff(before.splitlines(),
                                              after.splitlines(), lineterm="", n=0)
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


# --- the value round-trips -------------------------------------------------

def test_value_with_colon_space_parses_and_reads_back(tmp_path):
    value = "not done: needs capability wifi-ap-outage # bench-side"
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-01", f"reason={value}")
    assert code == 0
    assert entry(after, "TC-MQTT-01")["reason"] == value


def test_unsafe_value_is_written_as_folded_block(tmp_path):
    code, after = run(tmp_path, PLAN, "set", "JRN-01.1", "reason=needs: a peer")
    assert code == 0
    assert "    reason: >-\n      needs: a peer\n" in after


@pytest.mark.parametrize("value", ["yes", "123", "null", "2026-11-03", "- x",
                                   "'quoted'", "x" * 200, ""])
def test_values_yaml_would_reinterpret_read_back_as_strings(tmp_path, value):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", f"note={value}")
    assert code == 0
    assert entry(after, "TC-MQTT-02")["note"] == value


def test_long_value_wraps_and_still_reads_back(tmp_path):
    value = " ".join(f"word{i}" for i in range(60))
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", f"reason={value}")
    assert code == 0
    assert entry(after, "TC-MQTT-02")["reason"] == value
    assert max(len(ln) for ln in after.splitlines()) <= 80


# --- the diff touches only what changed -----------------------------------

def test_no_change_leaves_file_byte_identical(tmp_path, capsys):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-01", "status=failed",
                      "commit=1a2b3c4", "reason=broker refused the connection")
    assert code == 0
    assert after == PLAN
    assert "no change" in capsys.readouterr().out


def test_set_touches_only_the_changed_field_lines(tmp_path):
    code, after = run(tmp_path, PLAN, "set", "JRN-01.1", "status=successful",
                      "commit=9f8e7d6", "impl=tests/bench/test_journey.py::test_jrn_01")
    assert code == 0
    assert changed_lines(PLAN, after) == [
        "-    status: not done",
        "+    status: successful",
        "-    impl:",
        "+    impl: tests/bench/test_journey.py::test_jrn_01",
        "+    commit: 9f8e7d6",
    ]


def test_replacing_a_block_scalar_keeps_the_next_field(tmp_path):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-01", "reason=flaky")
    assert code == 0
    assert changed_lines(PLAN, after) == [
        "-    reason: >-",
        "-      broker refused the connection",
        "+    reason: flaky",
    ]
    assert entry(after, "TC-MQTT-01")["commit"] == "1a2b3c4"


def test_new_field_on_last_entry_without_trailing_newline(tmp_path):
    code, after = run(tmp_path, PLAN.rstrip("\n"), "set", "TC-MQTT-02",
                      "commit=abc1234")
    assert code == 0
    assert after.endswith("    status: successful\n    commit: abc1234\n")


def test_crlf_line_endings_are_kept(tmp_path):
    plan = tmp_path / "test-plan.yaml"
    plan.write_bytes(PLAN.replace("\n", "\r\n").encode())
    assert pe.main(["--plan", str(plan), "set", "JRN-01.1", "commit=abc"]) == 0
    raw = plan.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert b"    commit: abc\r\n" in raw


# --- --append -------------------------------------------------------------

def test_append_adds_to_existing_text_with_one_separator(tmp_path):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-01", "status=successful",
                      "--append", "reason=fixed in 2b3c4d5")
    assert code == 0
    e = entry(after, "TC-MQTT-01")
    assert e["reason"] == "broker refused the connection fixed in 2b3c4d5"
    assert e["status"] == "successful"


def test_append_to_absent_field_sets_it(tmp_path):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", "--append", "note=hi")
    assert code == 0
    assert entry(after, "TC-MQTT-02")["note"] == "hi"


# --- refusals: non-zero, nothing written ----------------------------------

@pytest.mark.parametrize("argv", [
    ["set", "TC-NOPE-01", "status=failed"],              # unknown id
    ["set", "TC-MQTT-01", "status=passed"],              # not in the vocabulary
    ["set", "TC-MQTT-01", "status=Failed"],
    ["set", "TC-MQTT-01", "id=TC-MQTT-09"],              # ids are never renumbered
    ["set", "TC-MQTT-01", "reason"],                     # not key=value
    ["set", "TC-MQTT-01", "a=1", "a=2"],                 # one key twice
    ["set", "TC-MQTT-01", "verifies=x", "--append", "verifies=y"],
    ["set", "JRN-01.1", "--append", "verifies=REQ-002"], # not text
    ["set", "TC-MQTT-01", "reason=line one\n line two"], # would not read back
    ["show", "TC-NOPE-01"],
])
def test_refusals_exit_nonzero_and_leave_file_unchanged(tmp_path, argv):
    code, after = run(tmp_path, PLAN, *argv)
    assert code != 0
    assert after == PLAN


@pytest.mark.parametrize("status", ["not done", "successful", "failed",
                                    "deprecated", "superseded"])
def test_every_defined_status_is_accepted(tmp_path, status):
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", f"status={status}")
    assert code == 0
    assert entry(after, "TC-MQTT-02")["status"] == status


def test_duplicate_id_is_refused(tmp_path, capsys):
    dup = PLAN + "  - id: TC-MQTT-02\n    status: failed\n"
    code, after = run(tmp_path, dup, "set", "TC-MQTT-02", "status=failed")
    assert code != 0
    assert after == dup
    assert "2 entries" in capsys.readouterr().err


def test_plan_that_does_not_parse_is_refused(tmp_path):
    broken = PLAN + "  - id: [unclosed\n"
    code, after = run(tmp_path, broken, "set", "TC-MQTT-02", "status=failed")
    assert code != 0
    assert after == broken


def test_missing_pyyaml_is_reported_and_nothing_written(tmp_path, monkeypatch,
                                                         capsys):
    monkeypatch.setitem(sys.modules, "yaml", None)
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", "status=failed")
    assert code != 0
    assert after == PLAN
    assert "PyYAML" in capsys.readouterr().err


def test_edit_that_would_not_read_back_is_refused(tmp_path, monkeypatch, capsys):
    # Force the renderer to produce text that parses to the wrong value: the
    # read-back check, not the renderer, is what keeps the file safe.
    monkeypatch.setattr(pe, "render_field",
                        lambda key, value, col, nl: [f"{key}: wrong{nl}"])
    code, after = run(tmp_path, PLAN, "set", "TC-MQTT-02", "status=failed")
    assert code != 0
    assert after == PLAN
    assert "read back" in capsys.readouterr().err


# --- other layouts --------------------------------------------------------

def test_other_layout_is_edited_in_place(tmp_path):
    code, after = run(tmp_path, OTHER_LAYOUT, "set", "A-1", "status=failed",
                      "reason=a b: c")
    assert code == 0
    assert entry(after, "A-1") == {"name": "first", "id": "A-1",
                                   "status": "failed", "reason": "a b: c"}
    assert changed_lines(OTHER_LAYOUT, after) == [
        "-            status: not done",
        "+            status: failed",
        "+            reason: >-",
        "+              a b: c",
    ]


def test_other_layout_first_key_on_the_dash_line(tmp_path):
    code, after = run(tmp_path, OTHER_LAYOUT, "set", "A-1", "name=renamed")
    assert code == 0
    assert "        -   name: renamed\n" in after
    assert entry(after, "A-1")["name"] == "renamed"


# --- show -----------------------------------------------------------------

def test_show_prints_the_entry_as_written(tmp_path, capsys):
    code, _ = run(tmp_path, PLAN, "show", "TC-MQTT-01")
    assert code == 0
    assert capsys.readouterr().out == (
        "  - id: TC-MQTT-01\n"
        "    kind: standard\n"
        '    status: "failed"\n'
        "    reason: >-\n"
        "      broker refused the connection\n"
        "    commit: 1a2b3c4\n"
    )
