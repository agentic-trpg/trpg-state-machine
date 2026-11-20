"""Plan a scoped SM pytest run from Git changes and locally visible dependencies."""

from __future__ import annotations

import argparse
import ast
import os
import posixpath
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMAND = "uv run --locked python -m pytest -q"
RUNNER = "tools/run_affected_tests.py"
SOURCE_AUDIT = "tests/test_domain_runtime.py"
RE_PIN_FILES = {"pyproject.toml", "uv.lock"}
RE_PACKAGES = {"dnd5e-engine", "dnd5e-srd-data"}
RE_PIN_TESTS = (
    "tests/test_lockable_migration.py::test_public_example_uses_common_pump_and_new_process_restore",
    "tests/test_item_transfers.py::test_public_item_transfer_example_uses_bound_ruleset",
    "tests/test_world_example.py::test_public_world_station_survives_process_boundary_and_continues",
)
TEST_ROOTS = ("tests/", "tools/tests/")
INPUT_ROOTS = ("src/", "tests/", "examples/", "tools/")
ASSET_SUFFIXES = {".json", ".yaml", ".yml"}
PROJECT_CONFIG = {
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "setup.cfg",
    "tox.ini",
}
DYNAMIC_IMPORTS = {
    "__import__",
    "builtins.__import__",
    "importlib.import_module",
    "runpy.run_module",
}
DYNAMIC_LOADERS = {
    "eval",
    "exec",
    "builtins.eval",
    "builtins.exec",
    "importlib.util.spec_from_file_location",
    "importlib.machinery.SourceFileLoader",
    "importlib.metadata.entry_points",
}


@dataclass
class Plan:
    """Record the changed paths, candidate pytest paths and reasons to stop."""

    changed: list[str]
    tests: dict[str, list[str]] = field(default_factory=dict)
    full: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    re_pins: list[str] = field(default_factory=list)


def git(root: Path, *args: str) -> str:
    """Read Git output, preserving NUL delimiters for path lists."""

    return os.fsdecode(subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE))


def re_pin_sources(root: Path, path: str, revision: str | None) -> dict[str, object] | None:
    """Read the RE Git sources from the worktree or a Git blob. Return None for invalid TOML."""

    try:
        if revision is None:
            content = (root / path).read_text(encoding="utf-8")
        else:
            content = git(root, "show", f"{revision}:{path}")
        document = tomllib.loads(content)
        if path == "pyproject.toml":
            sources = document.get("tool", {}).get("uv", {}).get("sources", {})
            return {
                name: source
                for name, source in sources.items()
                if name in RE_PACKAGES and isinstance(source, dict) and "git" in source
            }
        return {
            package["name"]: package["source"]["git"]
            for package in document.get("package", [])
            if package.get("name") in RE_PACKAGES and "git" in package.get("source", {})
        }
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    except (tomllib.TOMLDecodeError, UnicodeError, TypeError, AttributeError):
        return None


def changed_files(root: Path, base: str | None) -> tuple[list[str], list[str]]:
    """Collect changed paths and identify touched configuration files with a changed RE pin."""

    diff_args = ("--name-only", "--no-renames", "-z")
    unstaged = set(git(root, "diff", *diff_args).split("\0"))
    staged = set(git(root, "diff", "--cached", *diff_args).split("\0"))
    untracked = set(
        git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            ":(exclude,top).worktrees/**",
            ":(exclude,top,glob)*.txt",
        ).split("\0")
    )
    changed = (unstaged | staged | untracked) - {""}
    comparisons = [("", None, unstaged | untracked), ("HEAD", "", staged)]
    committed_before = "HEAD^1"
    if base is not None:
        committed_before = git(
            root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"
        ).strip()
        committed = git(root, "diff", *diff_args, committed_before, "HEAD", "--")
    elif not changed:
        committed = git(
            root,
            "diff-tree",
            "--root",
            "--diff-merges=first-parent",
            "--no-commit-id",
            "-r",
            *diff_args,
            "HEAD",
        )
    else:
        committed = ""
    committed_paths = set(committed.split("\0")) - {""}
    comparisons.append((committed_before, "HEAD", committed_paths))
    changed_pins = set()
    for before, after, paths in comparisons:
        for path in (paths & RE_PIN_FILES) - changed_pins:
            old = re_pin_sources(root, path, before)
            new = re_pin_sources(root, path, after)
            if old is not None and new is not None and old != new:
                changed_pins.add(path)
    return sorted(changed | committed_paths), sorted(changed_pins)


def is_test(path: str) -> bool:
    """Recognize default pytest filenames under SM's runtime and tooling test roots."""

    name = PurePosixPath(path).name
    return (
        path.startswith(TEST_ROOTS)
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def module_name(path: str) -> str:
    """Return the dotted import name, using src as a Python import root."""

    parts = PurePosixPath(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def string_literal(node: ast.AST) -> str | None:
    """Return the contents of a string literal."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def local_path(
    node: ast.AST, source: PurePosixPath, bindings: dict[str, PurePosixPath]
) -> PurePosixPath | None:
    """Resolve repo-relative literals and simple pathlib expressions anchored at __file__."""

    literal = string_literal(node)
    if literal and literal.replace("\\", "/").startswith(INPUT_ROOTS):
        return PurePosixPath(literal.replace("\\", "/"))
    if isinstance(node, ast.Name):
        return source if node.id == "__file__" else bindings.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        path = local_path(node.value, source, bindings)
        return path.parent if path is not None else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        index = node.slice
        if node.value.attr == "parents" and isinstance(index, ast.Constant):
            path = local_path(node.value.value, source, bindings)
            if (
                path is not None
                and type(index.value) is int
                and 0 <= index.value < len(path.parents)
            ):
                return path.parents[index.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        prefix = local_path(node.left, source, bindings)
        suffix = string_literal(node.right)
        if prefix is not None and suffix is not None:
            return prefix / suffix.replace("\\", "/")
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"Path", "str"}
            and len(node.args) == 1
        ):
            return local_path(node.args[0], source, bindings)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "with_name"}:
            path = local_path(node.func.value, source, bindings)
            if path is not None and node.func.attr == "resolve":
                return path
            if path is not None and len(node.args) == 1:
                name = string_literal(node.args[0])
                if name and "/" not in name and "\\" not in name and path.name:
                    return path.with_name(name)
    return None


def dotted_name(node: ast.AST) -> str:
    """Read the dotted spelling of a simple call target."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted_name(node.value)}.{node.attr}"
    return ""


def computed_path(
    node: ast.AST, source: PurePosixPath, bindings: dict[str, PurePosixPath]
) -> tuple[PurePosixPath, ast.expr] | None:
    """Return a known directory paired with a computed child path."""

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = local_path(node.left, source, bindings)
        if base is not None and string_literal(node.right) is None:
            return base, node.right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_name"
        and len(node.args) == 1
        and string_literal(node.args[0]) is None
    ):
        base = local_path(node.func.value, source, bindings)
        if base is not None:
            return base.parent, node.args[0]
    return None


def python_dependencies(
    tree: ast.Module, path: str, changed: set[PurePosixPath]
) -> tuple[set[str], set[str], set[str]]:
    """Collect import names, anchored input paths and unsupported loading mechanisms."""

    source = PurePosixPath(path)
    module = module_name(path)
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    paths: set[str] = set()
    unknown: set[str] = set()
    bindings: dict[str, PurePosixPath] = {}
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = local_path(node.value, source, bindings)
            if value is not None:
                bindings.update(
                    (target.id, value) for target in node.targets if isinstance(target, ast.Name)
                )
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parents = package.split(".") if package else []
                if node.level > len(parents):
                    continue
                prefix = ".".join([*parents[: len(parents) - node.level + 1], prefix]).strip(".")
            names.add(prefix)
            for alias in node.names:
                qualified = f"{prefix}.{alias.name}".lstrip(".")
                names.add(qualified)
                aliases[alias.asname or alias.name] = qualified
    for node in nodes:
        if isinstance(node, ast.expr):
            ref = local_path(node, source, bindings)
            if ref is not None:
                paths.add(posixpath.normpath(ref.as_posix()))
            computed = computed_path(node, source, bindings)
            if computed is not None:
                directory, expression = computed
                directory = PurePosixPath(posixpath.normpath(directory.as_posix()))
                suffix = (
                    string_literal(expression.values[-1])
                    if isinstance(expression, ast.JoinedStr) and expression.values
                    else None
                )
                if any(
                    directory in target.parents and (suffix is None or target.name.endswith(suffix))
                    for target in changed
                ):
                    unknown.add(f"computed path under {directory}")
        if (
            isinstance(node, ast.Name)
            and node.id == "pytest_plugins"
            and isinstance(node.ctx, ast.Store)
        ):
            unknown.add("pytest plugin loading")
        if not isinstance(node, ast.Call):
            continue
        first, separator, tail = dotted_name(node.func).partition(".")
        loader = aliases.get(first, first) + separator + tail
        if loader in DYNAMIC_LOADERS:
            unknown.add(loader)
        elif loader in DYNAMIC_IMPORTS:
            name = string_literal(node.args[0]) if node.args else None
            if name and not name.startswith(".") and len(node.args) == 1 and not node.keywords:
                names.add(name)
            else:
                unknown.add(loader)
        elif loader == "runpy.run_path" and (
            not node.args or local_path(node.args[0], source, bindings) is None
        ):
            unknown.add(loader)
    return names, paths, unknown


def dependency_index(
    root: Path, files: set[str], candidates: list[str]
) -> tuple[dict[str, set[str]], list[str]]:
    """Index reverse imports, exact file references and implicit pytest inputs."""

    modules = defaultdict(set)
    reverse = defaultdict(set)
    unknown = []
    changed = {PurePosixPath(path) for path in candidates if path in files}
    python_files = sorted(path for path in files if path.endswith(".py"))
    for path in python_files:
        modules[module_name(path)].add(path)
    for path in python_files:
        source = PurePosixPath(path)
        tree = ast.parse((root / path).read_bytes(), filename=path)
        names, dependencies, unresolved = python_dependencies(tree, path, changed)
        if unresolved:
            unknown.append(f"{path}: unresolved dynamic loading ({', '.join(sorted(unresolved))})")
        for imported in names:
            parts = imported.split(".")
            for length in range(1, len(parts) + 1):
                name = ".".join(parts[:length])
                dependencies.update(modules.get(name, ()))
                # Pytest's default import mode makes a test directory importable.
                # Directly executed examples likewise import from their script directory.
                if path.startswith(("tests/", "tools/tests/", "examples/")):
                    parent = ".".join(source.parent.parts)
                    dependencies.update(modules.get(f"{parent}.{name}", ()))
                    if path.startswith("tests/"):
                        dependencies.update(modules.get(f"tests.{name}", ()))
        for parent in source.parents:
            dependencies.add((parent / "__init__.py").as_posix())
            if is_test(path):
                dependencies.add((parent / "conftest.py").as_posix())
        for dependency in (dependencies & files) - {path}:
            reverse[dependency].add(path)
    return reverse, unknown


def transitive_dependents(path: str, reverse: dict[str, set[str]]) -> set[str]:
    """Return the original file and the consumers reachable through known dependencies."""

    affected = {path}
    pending = [path]
    while pending:
        new = reverse.get(pending.pop(), set()) - affected
        affected.update(new)
        pending.extend(new)
    return affected


def build_plan(root: Path, changed: list[str]) -> Plan:
    """Select a supported scope or record why an explicit broader run is required."""

    plan = Plan(changed)
    candidates = []
    for path in changed:
        source = PurePosixPath(path)
        if path.startswith("docs/") or (len(source.parts) == 1 and source.suffix == ".md"):
            plan.notes.append(f"{path}: documentation")
        elif source.name == "conftest.py":
            plan.full.append(f"{path}: shared pytest fixtures or configuration")
        elif path in PROJECT_CONFIG:
            plan.full.append(f"{path}: project or pytest configuration")
        elif path.startswith("src/") and (
            source.suffix == ".sql" or path == "src/trpg_state_machine/storage_migrations.py"
        ):
            plan.full.append(f"{path}: database schema or migration")
        elif not (root / path).is_file():
            plan.full.append(f"{path}: deleted or unavailable input")
        else:
            candidates.append(path)

    if not candidates:
        return plan

    files = {
        path
        for path in git(
            root,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "src",
            "tests",
            "examples",
            "tools",
        ).split("\0")
        if PurePosixPath(path).suffix in ASSET_SUFFIXES | {".py"} and (root / path).is_file()
    }
    tests = {path for path in files if is_test(path)}
    try:
        reverse, unresolved = dependency_index(root, files, candidates)
    except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
        plan.full.append(f"Could not inspect local dependencies: {exc}")
        return plan
    plan.full.extend(unresolved)
    for path in candidates:
        if path not in files:
            plan.full.append(f"{path}: unknown test input")
            continue
        selected = tests & transitive_dependents(path, reverse)
        if path == RUNNER:
            selected.update(test for test in tests if test.startswith("tools/tests/"))
        if not selected:
            plan.full.append(f"{path}: no tests found through supported dependencies")
        source = PurePosixPath(path)
        # test_production_writers_and_dependency_direction reads every direct *.py
        # in this package. Its source audit alone cannot establish behavioral coverage.
        if (
            SOURCE_AUDIT in tests
            and source.parent.as_posix() == "src/trpg_state_machine"
            and source.suffix == ".py"
        ):
            selected.add(SOURCE_AUDIT)
        for test in sorted(selected):
            plan.tests.setdefault(test, []).append(path)
    default_tests = {test for test in tests if test.startswith("tests/")}
    if default_tests and default_tests <= plan.tests.keys():
        plan.full.append("The dependency closure includes every default SM test module.")
    return plan


def print_plan(plan: Plan, *, preview: bool) -> None:
    """Show the changed paths, selection reasons and execution decision."""

    print("Changed files:")
    for path in plan.changed or ["(none)"]:
        print(f"  {path}")
    if plan.re_pins:
        print(f"Public examples to run first (RE pin changed in {', '.join(plan.re_pins)}):")
        for test in RE_PIN_TESTS:
            print(f"  {test}")
    print("Candidate tests:" if plan.full else "Selected tests:")
    if not plan.tests:
        print("  (none)")
    for test, reasons in sorted(plan.tests.items()):
        print(f"  {test} <- {', '.join(reasons)}")
    for note in plan.notes:
        print(f"  {note}")
    if plan.full:
        if plan.re_pins:
            print("Full SM test scope required after the public examples:")
        else:
            print("Full SM test scope required. Scoped pytest will not run:")
        for reason in plan.full:
            print(f"  {reason}")
        if preview or not plan.re_pins:
            print(f"Run separately: {DEFAULT_COMMAND}")
        tooling = sorted(test for test in plan.tests if test.startswith("tools/tests/"))
        if tooling:
            print(f"Tooling tests: {DEFAULT_COMMAND} {' '.join(tooling)}")
    elif not plan.tests:
        print("No pytest tests required for this plan.")


def main() -> int:
    """Check RE pin examples first, then run a supported scope or request full SM pytest."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare this commit directly to HEAD, plus local edits")
    parser.add_argument("--plan", action="store_true", help="show the scope without running pytest")
    args = parser.parse_args()
    try:
        changed, re_pins = changed_files(ROOT, args.base)
        plan = build_plan(ROOT, changed)
        plan.re_pins = re_pins
    except subprocess.CalledProcessError as exc:
        parser.exit(
            2, f"Git could not determine the test scope: {os.fsdecode(exc.stderr).strip()}\n"
        )
    except FileNotFoundError as exc:
        parser.exit(2, f"Git could not determine the test scope: {exc}\n")

    print_plan(plan, preview=args.plan)
    if args.plan:
        return 0
    pytest_command = [sys.executable, "-m", "pytest", "-q"]
    if plan.re_pins:
        sys.stdout.flush()
        status = subprocess.call([*pytest_command, *RE_PIN_TESTS], cwd=ROOT)
        if status:
            return status
    if plan.full:
        if plan.re_pins:
            print(f"Public examples passed. Run separately: {DEFAULT_COMMAND}")
        return 2
    if not plan.tests:
        return 0
    sys.stdout.flush()
    return subprocess.call([*pytest_command, *sorted(plan.tests)], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
