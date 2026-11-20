"""Exercise the SM affected-test CLI against real Git repositories and pytest."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "run_affected_tests.py"


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "tools" / RUNNER.name), *args],
        cwd=root / "tests",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "SM tests")
    git(tmp_path, "config", "commit.gpgsign", "false")
    write(tmp_path, ".gitignore", "__pycache__/\n.pytest_cache/\n")
    write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (tmp_path / "tools").mkdir()
    shutil.copyfile(RUNNER, tmp_path / "tools" / RUNNER.name)
    write(tmp_path, "tests/test_selected.py", "def test_selected():\n    assert True\n")
    write(tmp_path, "tests/test_unrelated.py", "def test_unrelated():\n    assert False\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def test_dirty_and_clean_tree_paths_with_exact_base(repo):
    original = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "branch", "--show-current")
    write(repo, "tests/test_selected.py", "def test_selected():\n    assert 1\n")
    git(repo, "add", "tests/test_selected.py")
    git(repo, "commit", "-qm", "change selected test")
    write(repo, ".worktrees/local/keep.txt", "untracked worktree artifact")
    write(repo, "本地笔记.txt", "untracked local note")
    clean = run(repo, "--plan")
    assert clean.returncode == 0, clean.stderr
    assert "tests/test_selected.py <- tests/test_selected.py" in clean.stdout
    assert ".worktrees" not in clean.stdout
    assert "本地笔记.txt" not in clean.stdout

    write(repo, "docs/staged.md", "staged documentation")
    git(repo, "add", "docs/staged.md")
    write(repo, "README.md", "first edit")
    git(repo, "add", "README.md")
    write(repo, "README.md", "unstaged edit to a tracked path")
    write(repo, "tests/test_selected.py", "def test_selected():\n    assert 2\n")
    unstaged = run(repo, "--plan", "--base", "HEAD")
    assert "tests/test_selected.py <- tests/test_selected.py" in unstaged.stdout
    write(repo, "docs/untracked ü file.md", "untracked documentation")
    write(repo, ".worktrees/tracked.txt", "tracked despite the directory")
    git(repo, "add", ".worktrees/tracked.txt")
    write(repo, "tracked.txt", "staged root text input")
    git(repo, "add", "tracked.txt")
    dirty = run(repo, "--plan", "--base", original)
    assert dirty.returncode == 0, dirty.stderr
    assert ".worktrees/tracked.txt: unknown test input" in dirty.stdout
    assert "  tracked.txt: unknown test input" in dirty.stdout
    assert ".worktrees/local" not in dirty.stdout
    for path in ("docs/staged.md", "README.md", "docs/untracked ü file.md"):
        assert f"{path}: documentation" in dirty.stdout
    assert "tests/test_selected.py <- tests/test_selected.py" in dirty.stdout

    # A ref ahead of HEAD is compared directly, even when its merge base is HEAD.
    git(repo, "add", "README.md", "docs", "tests/test_selected.py")
    git(repo, "commit", "-qm", "save local changes")
    git(repo, "switch", "-qc", "comparison")
    write(repo, "tests/test_unrelated.py", "def test_unrelated():\n    assert 3\n")
    git(repo, "add", "tests/test_unrelated.py")
    git(repo, "commit", "-qm", "independent changes on comparison branch")
    git(repo, "switch", "-q", branch)
    compare = run(repo, "--plan", "--base", "comparison")
    assert compare.returncode == 0, compare.stderr
    assert "tests/test_unrelated.py <- tests/test_unrelated.py" in compare.stdout
    invalid = run(repo, "--plan", "--base", "missing-ref")
    assert invalid.returncode == 2
    assert "Git could not determine the test scope" in invalid.stderr


def test_runner_executes_only_selected_files_and_returns_pytest_status(repo):
    write(repo, "tests/test_selected.py", "def test_selected():\n    assert True  # edited\n")
    success = run(repo)
    assert success.returncode == 0, success.stdout + success.stderr
    assert "1 passed" in success.stdout
    assert "test_unrelated.py" not in success.stdout
    write(repo, "tests/test_selected.py", "def test_selected():\n    assert False\n")
    planned = run(repo, "--plan")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "FAILED" not in planned.stdout
    failure = run(repo)
    assert failure.returncode == 1, failure.stdout + failure.stderr
    assert "1 failed" in failure.stdout and "test_unrelated.py" not in failure.stdout
    write(repo, "tests/test_selected.py", "# no tests to collect\n")
    assert run(repo).returncode == 5


def test_collects_test_helper_and_anchored_example_fixture_consumers(repo):
    write(repo, "tests/support.py", "HELPER = 1\n")
    write(repo, "examples/data.json", "{}")
    write(
        repo,
        "examples/demo.py",
        'from pathlib import Path\nPath(__file__).with_name("data.json")\n',
    )
    write(
        repo,
        "tests/test_selected.py",
        "from support import HELPER\nfrom pathlib import Path\n"
        "from runpy import run_path\n"
        'run_path(str(Path(__file__).parents[1] / "examples" / "demo.py"))\n'
        "def test_selected():\n    assert HELPER\n",
    )
    write(repo, "tests/fixtures/data.json", "{}")
    write(repo, "tests/test_unrelated.py", 'def test_unrelated():\n    assert "data.json" == 0\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add helper and example inputs")
    write(repo, "tests/support.py", "HELPER = 2\n")
    write(repo, "examples/data.json", '{"changed": true}')
    result = run(repo, "--plan")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_selected.py <- examples/data.json, tests/support.py" in result.stdout
    assert "test_unrelated.py" not in result.stdout
    assert "Full SM test scope required" not in result.stdout

    write(repo, "tests/fixtures/data.json", '{"also changed": true}')
    unknown = run(repo, "--plan")
    assert "tests/fixtures/data.json: no tests found" in unknown.stdout
    assert "test_unrelated.py" not in unknown.stdout

    write(repo, "tests/fixtures/data.json", "{}")
    write(
        repo,
        "tests/test_unrelated.py",
        "from pathlib import Path\n"
        "def test_unrelated():\n"
        '    name = "data"\n'
        '    path = Path(__file__).parents[1] / "examples" / f"{name}.json"\n'
        "    assert path.exists()\n",
    )
    dynamic = run(repo, "--plan")
    assert "tests/test_unrelated.py: unresolved dynamic loading (computed path under examples)" in (
        dynamic.stdout
    )
    assert "Full SM test scope required" in dynamic.stdout


@pytest.mark.parametrize(
    ("path", "content", "reason"),
    [
        ("tests/conftest.py", "", "shared pytest fixtures"),
        ("uv.lock", "", "project or pytest configuration"),
        ("src/trpg_state_machine/_migration_55.sql", "", "database schema"),
        ("tests/fixtures/custom.bin", "", "unknown test input"),
        ("tests/test_unrelated.py", None, "deleted or unavailable input"),
    ],
)
def test_full_scope_does_not_accidentally_run_pytest(repo, path, content, reason):
    write(repo, "tests/test_selected.py", 'raise AssertionError("pytest started")\n')
    if content is None:
        (repo / path).unlink()
    else:
        write(repo, path, content)
    for args, expected in ((("--plan",), 0), ((), 2)):
        broad = run(repo, *args)
        assert broad.returncode == expected, broad.stdout + broad.stderr
        assert reason in broad.stdout and "pytest started" not in broad.stdout


PUBLIC_EXAMPLES = (
    (
        "tests/test_lockable_migration.py",
        "test_public_example_uses_common_pump_and_new_process_restore",
    ),
    ("tests/test_item_transfers.py", "test_public_item_transfer_example_uses_bound_ruleset"),
    (
        "tests/test_world_example.py",
        "test_public_world_station_survives_process_boundary_and_continues",
    ),
)


def test_re_pin_prioritizes_only_public_example_nodes_before_full_scope(repo):
    """Run only the prioritized nodes, and preserve their pytest failure status."""

    lock = """\
[[package]]
name = "dnd5e-engine"
source = { git = "https://example.invalid/re#old" }
"""
    write(repo, "uv.lock", lock)
    for path, node in PUBLIC_EXAMPLES:
        write(
            repo,
            path,
            f"def {node}():\n    assert True\n\ndef test_out_of_scope():\n    assert False\n",
        )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add lock and public example tests")

    unrelated = lock + '\n[[package]]\nname = "unrelated"\nsource = { git = "other#new" }\n'
    write(repo, "uv.lock", unrelated)
    write(
        repo,
        "pyproject.toml",
        (repo / "pyproject.toml").read_text(encoding="utf-8") + "addopts = '-ra'\n",
    )
    broad = run(repo)
    assert broad.returncode == 2, broad.stdout + broad.stderr
    assert "Scoped pytest will not run" in broad.stdout

    write(repo, "uv.lock", unrelated.replace("re#old", "re#new"))
    preview = run(repo, "--plan")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "RE pin changed in uv.lock" in preview.stdout
    for path, node in PUBLIC_EXAMPLES:
        assert f"{path}::{node}" in preview.stdout
    assert "3 passed" not in preview.stdout

    success = run(repo)
    assert success.returncode == 2, success.stdout + success.stderr
    assert success.stdout.index("3 passed") < success.stdout.index("Run separately:")
    path, node = PUBLIC_EXAMPLES[0]
    write(repo, path, f"def {node}():\n    assert False\n")
    failure = run(repo)
    assert failure.returncode == 1, failure.stdout + failure.stderr
    assert "1 failed, 2 passed" in failure.stdout
    assert "Full SM test scope required" in failure.stdout
    assert "Run separately:" not in failure.stdout


def test_re_pin_respects_staged_unstaged_and_committed_comparisons(repo):
    """Use the same Git endpoints as the changed-path list for RE source changes."""

    original = (repo / "pyproject.toml").read_text(encoding="utf-8")
    original += (
        "\n[tool.uv.sources]\n"
        'dnd5e-engine = { git = "https://example.invalid/re", rev = "old" }\n'
        'dnd5e-srd-data = { git = "https://example.invalid/re", rev = "old" }\n'
    )
    write(repo, "pyproject.toml", original)
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "-qm", "add RE sources")
    base = git(repo, "rev-parse", "HEAD")
    updated = original.replace('rev = "old"', 'rev = "new"')

    write(repo, "pyproject.toml", updated)
    assert "RE pin changed in pyproject.toml" in run(repo, "--plan").stdout
    git(repo, "add", "pyproject.toml")
    write(repo, "pyproject.toml", updated + "# unrelated unstaged change\n")
    assert "RE pin changed in pyproject.toml" in run(repo, "--plan").stdout
    write(repo, "pyproject.toml", updated)
    git(repo, "commit", "-qm", "update both RE package pins")
    assert "RE pin changed in pyproject.toml" in run(repo, "--plan").stdout
    assert "RE pin changed" not in run(repo, "--plan", "--base", "HEAD").stdout
    write(repo, "README.md", "# unrelated local edit\n")
    assert "RE pin changed" not in run(repo, "--plan").stdout
    assert "RE pin changed in pyproject.toml" in run(repo, "--plan", "--base", base).stdout


def test_unresolved_dynamic_consumer_blocks_partial_source_selection(repo):
    write(repo, "src/local_api.py", "VALUE = 1\n")
    write(repo, "tests/test_selected.py", "import local_api\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add static consumer")
    write(repo, "src/local_api.py", "VALUE = 2\n")
    write(
        repo,
        "tests/helper.py",
        "from importlib import import_module as load\n"
        'def consume(name="local_api"):\n    return load(name)\n',
    )
    result = run(repo, "--plan")
    assert result.returncode == 0, result.stderr
    assert "tests/test_selected.py <- src/local_api.py" in result.stdout
    assert "tests/helper.py: unresolved dynamic loading (importlib.import_module)" in result.stdout
    assert "Full SM test scope required" in result.stdout


def test_source_audit_supplements_behavioral_tests_without_narrowing_unknown_scope(repo):
    package = "src/trpg_state_machine"
    known = f"{package}/runtime.py"
    unknown = f"{package}/new_runtime.py"
    audit = "tests/test_domain_runtime.py"
    write(repo, known, "VALUE = 1\n")
    write(repo, "tests/test_selected.py", "from trpg_state_machine import runtime\n")
    write(
        repo,
        audit,
        "from pathlib import Path\n"
        "def test_production_writers_and_dependency_direction():\n"
        '    root = Path(__file__).parents[1] / "src/trpg_state_machine"\n'
        '    for path in root.glob("*.py"):\n'
        "        assert path.read_text()\n",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add source audit and runtime consumer")
    write(repo, known, "VALUE = 2\n")
    planned = run(repo, "--plan")
    assert planned.returncode == 0, planned.stderr
    assert f"{audit} <- {known}" in planned.stdout
    assert f"tests/test_selected.py <- {known}" in planned.stdout
    assert "Full SM test scope required" not in planned.stdout

    write(repo, known, "VALUE = 1\n")
    write(repo, unknown, "VALUE = 3\n")
    broad = run(repo)
    assert broad.returncode == 2, broad.stdout + broad.stderr
    assert f"{audit} <- {unknown}" in broad.stdout
    assert f"{unknown}: no tests found through supported dependencies" in broad.stdout
    assert "tests/test_selected.py" not in broad.stdout


def test_documentation_only_and_explicit_empty_scope(repo):
    empty = run(repo, "--plan", "--base", "HEAD")
    assert empty.returncode == 0, empty.stderr
    assert "(none)" in empty.stdout and "No pytest tests required" in empty.stdout
    write(repo, "README.md", "# documentation only\n")
    docs = run(repo)
    assert docs.returncode == 0, docs.stdout + docs.stderr
    assert "README.md: documentation" in docs.stdout
    assert "No pytest tests required" in docs.stdout
