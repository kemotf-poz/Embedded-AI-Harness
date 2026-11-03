#!/usr/bin/env python3
"""Read or edit one entry of the test plan, and never save a plan that doesn't parse.

    plan_entry.py [--plan PATH] show <ID>
    plan_entry.py [--plan PATH] set <ID> key=value ... [--append key=value ...]

An entry is the mapping whose `id:` is <ID>, wherever it sits in the file.
`set` rewrites only the lines of the fields it changes; every other byte stays
as it was. Before writing, the whole new text is parsed and the entry read
back; if either fails, nothing is written. Exit 0 = written or no change,
non-zero = nothing written.

Text editing is standard library; parsing needs PyYAML (`python3-yaml`).
"""
import argparse
import os
import re
import sys
import tempfile

DEFAULT_PLAN = "testing/test-plan.yaml"

# The test statuses the build skill defines (references/test-lifecycle.md §1),
# plus the two for retired tests (references/test-design.md §5a).
STATUSES = ("not done", "successful", "failed", "deprecated", "superseded")

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")
WIDTH = 80


class Refused(Exception):
    """The edit cannot be made safely; nothing is written."""


# ---- locating the entry --------------------------------------------------

def find_entries(root, test_id, yaml):
    """Every mapping node whose `id` scalar is test_id, in document order."""
    found, seen, stack = [], set(), [root]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            if any(isinstance(k, yaml.ScalarNode) and k.value == "id"
                   and isinstance(v, yaml.ScalarNode) and v.value == test_id
                   for k, v in node.value):
                found.append(node)
            stack.extend(n for pair in reversed(node.value) for n in reversed(pair))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(reversed(node.value))
    return found


def is_filler(line, col):
    """A blank line, or a comment no deeper than the entry's keys."""
    s = line.strip()
    return not s or (s.startswith("#") and len(line) - len(line.lstrip()) <= col)


def field_spans(entry, lines):
    """{key: (first_line, last_line, column)} for each field of a block mapping.

    A field ends where its value ends, never past the next field, with
    trailing blank and comment lines left outside it."""
    spans = {}
    pairs = entry.value
    for i, (k, v) in enumerate(pairs):
        first, col = k.start_mark.line, k.start_mark.column
        empty = v.start_mark.index == v.end_mark.index
        last = first if empty else v.end_mark.line - (v.end_mark.column == 0)
        if i + 1 < len(pairs):
            last = min(last, pairs[i + 1][0].start_mark.line - 1)
        last = max(last, first)
        while last > first and is_filler(lines[last], col):
            last -= 1
        spans[k.value] = (first, last, col)
    return spans


def load(text, test_id, yaml):
    """(entry node, its constructed value) for the one entry with this id."""
    loader = yaml.SafeLoader(text)
    try:
        root = loader.get_single_node()
        entries = find_entries(root, test_id, yaml)
        if not entries:
            raise Refused(f"no entry has id {test_id}")
        if len(entries) > 1:
            where = ", ".join(str(e.start_mark.line + 1) for e in entries)
            raise Refused(f"{len(entries)} entries have id {test_id} (lines {where})")
        node = entries[0]
        if node.flow_style:
            raise Refused(f"{test_id} is a flow mapping; edit it by hand")
        return node, loader.construct_object(node, deep=True)
    finally:
        loader.dispose()


# ---- rendering a value ---------------------------------------------------

def is_plain(key, value, col, yaml):
    if not value or value != value.strip() or value[0] in PLAIN_START:
        return False
    if ": " in value or " #" in value or value.endswith(":"):
        return False
    if col + len(key) + 2 + len(value) > WIDTH:
        return False
    try:
        return yaml.safe_load(f"k: {value}") == {"k": value}
    except yaml.YAMLError:
        return False


def fold(value, width):
    """Split at single spaces so a `>-` scalar folds back to value."""
    words = value.split(" ")
    if "" in words:
        return [value]
    out = [words[0]]
    for w in words[1:]:
        if len(out[-1]) + 1 + len(w) > width:
            out.append(w)
        else:
            out[-1] += " " + w
    return out


def render_field(key, value, col, nl):
    """The field's lines; the first carries no indent (the caller's prefix)."""
    import yaml
    if value == "":
        return [f"{key}: ''{nl}"]
    if is_plain(key, value, col, yaml):
        return [f"{key}: {value}{nl}"]
    pad = " " * (col + 2)
    return [f"{key}: >-{nl}"] + [f"{pad}{ln}{nl}" for ln in fold(value, WIDTH - len(pad))]


# ---- the edit ------------------------------------------------------------

def parse_assignments(items):
    out = []
    for item in items:
        key, eq, value = item.partition("=")
        if not eq or not KEY.fullmatch(key):
            raise Refused(f"expected key=value, got {item!r}")
        if "\n" in value or "\r" in value:
            raise Refused(f"{key}: a value must be one line")
        out.append((key, value))
    return out


def requested_values(sets, appends, current):
    """{key: new value}, in argument order; appends join with one space."""
    wanted = {}
    for key, value in sets + appends:
        if key in wanted:
            raise Refused(f"{key} is given more than once")
        if key == "id":
            raise Refused("ids are never changed; declare a new entry by hand")
        wanted[key] = value
    for key, value in appends:
        old = current.get(key)
        if old is not None and not isinstance(old, str):
            raise Refused(f"{key} is not text; --append adds to text only")
        wanted[key] = f"{old} {value}" if old else value
    status = wanted.get("status")
    if status is not None and status not in STATUSES:
        raise Refused(f"status {status!r} is not one of: {', '.join(STATUSES)}")
    return wanted


def edit(text, test_id, sets, appends, yaml):
    """The new text, or None when every field already has its value."""
    node, current = load(text, test_id, yaml)
    wanted = requested_values(sets, appends, current)
    changes = {k: v for k, v in wanted.items()
               if k not in current or current[k] != v}
    if not changes:
        return None

    lines = text.splitlines(keepends=True)
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    spans = field_spans(node, lines)
    end = max(last for _, last, _ in spans.values())
    col = node.value[0][0].start_mark.column

    inserted = []
    for key, value in changes.items():
        if key not in spans:
            new = render_field(key, value, col, nl)
            inserted += [" " * col + new[0]] + new[1:]
    if inserted and not lines[end].endswith(("\n", "\r")):
        lines[end] += nl
    lines[end + 1:end + 1] = inserted

    for key in sorted((k for k in changes if k in spans),
                      key=lambda k: spans[k][0], reverse=True):
        first, last, kcol = spans[key]
        new = render_field(key, changes[key], kcol, nl)
        new[0] = lines[first][:kcol] + new[0]
        if not lines[last].endswith(("\n", "\r")):
            new[-1] = new[-1].rstrip("\r\n")
        lines[first:last + 1] = new

    new_text = "".join(lines)
    try:
        _, after = load(new_text, test_id, yaml)
    except yaml.YAMLError as exc:
        raise Refused(f"the edited plan does not parse:\n{exc}")
    expected = {**current, **changes}
    if after != expected:
        bad = sorted(k for k in expected.keys() | after.keys()
                     if after.get(k) != expected.get(k))
        raise Refused(f"{test_id} does not read back as requested: {', '.join(bad)}")
    return new_text


def show(text, test_id, yaml):
    node, _ = load(text, test_id, yaml)
    lines = text.splitlines(keepends=True)
    end = max(last for _, last, _ in field_spans(node, lines).values())
    out = "".join(lines[node.start_mark.line:end + 1])
    return out if out.endswith("\n") else out + "\n"


def write(path, text):
    """Replace path in one step, so a failure never leaves half a plan."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".plan-entry-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="plan_entry.py", description="Read or edit one entry of the test plan.")
    ap.add_argument("--plan", default=DEFAULT_PLAN,
                    help=f"the plan file (default: {DEFAULT_PLAN})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_show = sub.add_parser("show", help="print one entry as it appears in the file")
    p_show.add_argument("id")
    p_set = sub.add_parser("set", help="change or add fields on one entry")
    p_set.add_argument("id")
    p_set.add_argument("assign", nargs="*", metavar="key=value")
    p_set.add_argument("--append", nargs="+", action="extend", default=[],
                       metavar="key=value", help="add to the field's text instead")
    args = ap.parse_args(argv)

    try:
        import yaml
    except ImportError:
        print("plan_entry: PyYAML is required (apt install python3-yaml, "
              "or pip install pyyaml); nothing written", file=sys.stderr)
        return 1

    try:
        with open(args.plan, encoding="utf-8", newline="") as f:
            text = f.read()
        if args.cmd == "show":
            sys.stdout.write(show(text, args.id, yaml))
            return 0
        if not args.assign and not args.append:
            raise Refused("set needs at least one key=value")
        new_text = edit(text, args.id, parse_assignments(args.assign),
                        parse_assignments(args.append), yaml)
        if new_text is None:
            print(f"{args.id}: no change")
            return 0
        write(args.plan, new_text)
        print(f"{args.id}: updated")
        return 0
    except Refused as exc:
        print(f"plan_entry: {exc}; nothing written", file=sys.stderr)
    except yaml.YAMLError as exc:
        print(f"plan_entry: {args.plan} does not parse; nothing written\n{exc}",
              file=sys.stderr)
    except OSError as exc:
        print(f"plan_entry: {exc}; nothing written", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
