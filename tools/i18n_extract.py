#!/usr/bin/env python3
"""Collect every translatable string of the app into a template catalog.

Scans app/**/*.py with the ast module for calls of tr() / _() / ntr() / N_()
(also i18n.tr(...) etc.) whose first argument is a string literal, and writes

    app/locales/_template.json          {"_meta": {...}, "<english>": "", ...}
                                        (plural keys: {"one": "", "other": ""})
    app/locales/_template_sources.json  {"<english>": {"sources": ["app/x.py:12"],
                                          "plural": "<english plural>"}, ...}

Usage (from the project root):
    python tools/i18n_extract.py            write the template, print a summary
    python tools/i18n_extract.py --check    also list per language how many
                                            keys are missing / obsolete and any
                                            placeholder mismatches
    python tools/i18n_extract.py --check -v   ... and print the missing keys

tr(variable) is allowed (mark the value with N_() where it is defined); a key
BUILT at the call (f-string, + concatenation, .format()) can't be extracted
and is reported as a warning - use tr("... {x} ...", x=x) instead.
"""
import argparse
import ast
import json
import os
import re
import string
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
LOCALES = os.path.join(APP, "locales")
FUNCS = {"tr", "_", "ntr", "N_"}


def _func_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _built_string(node):
    """An f-string, a + concatenation involving a literal, or "...".format()."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return any(isinstance(x, (ast.Constant, ast.JoinedStr)) and
                   isinstance(getattr(x, "value", ""), str) or isinstance(x, ast.JoinedStr)
                   for x in (node.left, node.right)) or _built_string(node.left)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"):
        return True
    return False


def scan_file(path, keys, sources, plurals, warnings):
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError) as exc:
        warnings.append(f"{rel}: can't parse ({exc})")
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _func_name(node)
        if name not in FUNCS or not node.args:
            continue
        text = _literal(node.args[0])
        where = f"{rel}:{node.lineno}"
        if text is None:
            # tr(variable) is fine (the value was N_()-marked where it was
            # defined); an f-string / concatenation / .format() key is a bug
            if _built_string(node.args[0]):
                warnings.append(f"{where}: {name}() with a built (non-literal) "
                                f"key ({type(node.args[0]).__name__}) - use "
                                "tr(\"... {x} ...\", x=...)")
            continue
        if not text.strip():
            continue
        keys.add(text)
        sources.setdefault(text, []).append(where)
        if name == "ntr" and len(node.args) > 1:
            pl = _literal(node.args[1])
            if pl is not None:
                plurals[text] = pl


def collect():
    keys, sources, plurals, warnings = set(), {}, {}, []
    for d, dirs, files in os.walk(APP):
        dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", "locales"))
        for fn in sorted(files):
            if fn.endswith(".py"):
                scan_file(os.path.join(d, fn), keys, sources, plurals, warnings)
    return keys, sources, plurals, warnings


def _fields(s):
    try:
        return {f for _t, f, _s, _c in string.Formatter().parse(s) if f}
    except ValueError:
        return {"<broken braces>"}


def write_template(keys, sources, plurals):
    os.makedirs(LOCALES, exist_ok=True)
    tpl = {"_meta": {"name": "<native language name>",
                     "english_name": "<English language name>"}}
    for k in sorted(keys):
        tpl[k] = {"one": "", "other": ""} if k in plurals else ""
    side = {}
    for k in sorted(keys):
        e = {"sources": sources.get(k, [])}
        if k in plurals:
            e["plural"] = plurals[k]
        side[k] = e
    for name, data in (("_template.json", tpl), ("_template_sources.json", side)):
        with open(os.path.join(LOCALES, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")


def check(keys, verbose):
    names = sorted(n for n in os.listdir(LOCALES)
                   if n.endswith(".json") and not n.startswith("_"))
    print(f"\n{'lang':8} {'translated':>10} {'missing':>8} {'obsolete':>9}  issues")
    for n in names:
        code = n[:-5]
        try:
            with open(os.path.join(LOCALES, n), "r", encoding="utf-8") as f:
                cat = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"{code:8} can't read: {exc}")
            continue
        entries = {k: v for k, v in cat.items() if not k.startswith("_")}
        done = {k for k, v in entries.items() if v}
        missing = sorted(keys - done)
        obsolete = sorted(set(entries) - keys)
        issues = []
        for k in sorted(done & keys):
            v = entries[k]
            vals = list(v.values()) if isinstance(v, dict) else [v]
            want = _fields(k)
            for t in vals:
                if isinstance(t, str) and t and not _fields(t) <= want | {"n"}:
                    issues.append(f"placeholders differ: {k!r}")
                    break
        if code == "en":
            missing = []
        print(f"{code:8} {len(done & keys):>10} {len(missing):>8} {len(obsolete):>9}  "
              f"{len(issues) or ''}")
        if verbose:
            for k in missing:
                print(f"    missing: {k!r}")
            for k in obsolete:
                print(f"    obsolete: {k!r}")
        for i in issues:
            print(f"    {i}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="report missing keys per language")
    ap.add_argument("-v", "--verbose", action="store_true", help="list the missing keys")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    keys, sources, plurals, warnings = collect()
    write_template(keys, sources, plurals)
    files = {s.split(":")[0] for v in sources.values() for s in v}
    words = sum(len(re.findall(r"\w+", k)) for k in keys)
    print(f"{len(keys)} keys ({len(plurals)} plural, ~{words} words) from "
          f"{len(files)} files -> app/locales/_template.json")
    for w in warnings:
        print("WARNING:", w)
    if a.check:
        check(keys, a.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
