#!/usr/bin/env python3
"""How many TPC drops survive each shared-state rule, on patches a gate already drops a
TPC test for. Development image, this checkout at /app, pinned tree at /opt/sqlglot:

    python /app/tools/mutation_rule_count.py GATE_OUTPUT.txt SET_DIR ...

GATE_OUTPUT.txt is tools/tpc_gate.py output; a line "<set>/<commit>: cand ... dropped [..]"
with a non-empty drop list marks a dropped patch. Rules:
  current  br.tpc.mutation_refusal (what the patch changes in names, stores, in-place
           operations, copy-like keywords)
  strict   refuse when any changed function, pinned or patched, contains any store or
           delete of an attribute, item or global, or calls a method named pop, popitem,
           append, extend, insert, remove, clear, update, setdefault, add, discard, sort
           or reverse
"""

import dis
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from br import analyze, tpc  # noqa: E402
from br.patch import apply  # noqa: E402

PINNED = Path("/opt/sqlglot")
STRICT_OPS = {"STORE_ATTR", "STORE_SUBSCR", "DELETE_ATTR", "DELETE_SUBSCR", "STORE_GLOBAL", "DELETE_GLOBAL"}
STRICT_METHODS = {"pop", "popitem", "append", "extend", "insert", "remove", "clear", "update", "setdefault",
                  "add", "discard", "sort", "reverse"}


def read(path):
    f = PINNED / path
    return f.read_bytes().decode("utf-8") if f.is_file() else None


def strict_refusal(changes) -> str | None:
    for ch in changes:
        if ch.old_path is None or ch.new_path != ch.old_path or not ch.old_path.endswith(".py"):
            continue
        old, new = analyze._compile(ch.old_text, ch.old_path), analyze._compile(ch.new_text, ch.new_path)
        todo = [(old, new)]
        while todo:
            o, n = todo.pop()
            ok, nk = analyze._keyed_children(o), analyze._keyed_children(n)
            todo.extend((ok[k], nk[k]) for k in ok if k in nk)
            if not o.co_flags & 0x1 or analyze._own_signature(o) == analyze._own_signature(n):
                continue
            for code in (o, n):
                for ins in dis.get_instructions(code):
                    if ins.opname in STRICT_OPS:
                        return f"{code.co_qualname}: {ins.opname} {ins.argrepr}"
                    if ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and str(ins.argval) in STRICT_METHODS:
                        return f"{code.co_qualname}: .{ins.argval}()"
    return None


def main() -> int:
    gate, sets = Path(sys.argv[1]), {Path(p).name: Path(p) for p in sys.argv[2:]}
    dropped = []
    for line in gate.read_text().splitlines():
        m = re.match(r"(\S+)/(\w+): cand \[[^\]]*\] failing \S+ dropped \[([^\]]+)\]", line)
        if m and m.group(1) in sets:
            dropped.append((m.group(1), m.group(2), len(m.group(3).split(","))))
    survive = {"current": [0, 0], "strict": [0, 0]}
    for set_name, commit, tests in dropped:
        text = (sets[set_name] / "commits" / f"{commit}.patch").read_text()
        changes = apply(text, read)
        cur, strict = tpc.mutation_refusal(changes), strict_refusal(changes)
        for rule, why in (("current", cur), ("strict", strict)):
            if why is None:
                survive[rule][0] += 1
                survive[rule][1] += tests
        print(f"{set_name}/{commit}: dropped {tests} | current {'keeps drop' if cur is None else 'refuses: ' + cur[:70]}"
              f" | strict {'keeps drop' if strict is None else 'refuses: ' + strict[:70]}")
    print(f"dropped patches {len(dropped)} ({sum(t for *_, t in dropped)} tests); surviving current rule: "
          f"{survive['current'][0]} patches ({survive['current'][1]} tests); surviving strict rule: "
          f"{survive['strict'][0]} patches ({survive['strict'][1]} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
