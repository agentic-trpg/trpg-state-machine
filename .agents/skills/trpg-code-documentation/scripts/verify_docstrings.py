"""Compare production Python ASTs after removing only genuine docstring nodes."""

import argparse
import ast
import collections
import json
import subprocess
from pathlib import Path

OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class WithoutDocstrings(ast.NodeTransformer):
    """Remove the first string expression only in legal docstring-owning nodes."""

    def generic_visit(self, node):
        """Strip genuine leading documentation and retain all executable expressions."""
        if isinstance(node, OWNERS) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
        return super().generic_visit(node)


def git(root, *args):
    """Read Git output, propagating an invalid repository or comparison revision."""
    return subprocess.check_output(["git", "-C", str(root), *args])


def normalized(source):
    """Parse without executing imports and return a location-independent semantic AST."""
    return ast.dump(WithoutDocstrings().visit(ast.parse(source)), include_attributes=False)


def baseline_sources(root, base, paths):
    """Read fixed Git blobs in one process without checking out or executing code."""
    requests = "".join(f"{base}:{name}\n" for name in paths).encode()
    data = subprocess.check_output(["git", "-C", str(root), "cat-file", "--batch"], input=requests)
    result = {}
    offset = 0
    for name in paths:
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"Missing baseline source: {name}")
        size = int(header[2])
        offset = end + 1
        result[name] = data[offset : offset + size].decode("utf-8-sig")
        offset += size + 1
    return result


def coverage(source):
    """Count real module, class and function documentation, including nested functions."""
    counts = collections.Counter()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, OWNERS):
            kind = (
                "module"
                if isinstance(node, ast.Module)
                else "class"
                if isinstance(node, ast.ClassDef)
                else "function"
            )
            counts[kind] += 1
            if ast.get_docstring(node):
                counts[f"documented_{kind}"] += 1
    return counts


def main():
    """Check every tracked production source against an explicit Git base."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    base = git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}").decode().strip()
    paths = git(root, "ls-files", "-z", "*.py").decode().split("\0")
    paths = [p for p in paths if "src" in Path(p).parts and "tests" not in Path(p).parts]
    originals = baseline_sources(root, base, paths)
    before, after = collections.Counter(), collections.Counter()
    changed, failures = [], []
    for name in paths:
        old = originals[name]
        new = (root / name).read_text(encoding="utf-8-sig")
        # compile also checks __future__ placement, which ast.parse alone permits.
        compile(new, name, "exec")
        before.update(coverage(old))
        after.update(coverage(new))
        if normalized(old) != normalized(new):
            failures.append(name)
        if old != new:
            changed.append(name)
    print(
        json.dumps(
            {
                "base": base,
                "files": len(paths),
                "before": before,
                "after": after,
                "changed": changed,
                "non_docstring_ast_changes": failures,
            },
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
