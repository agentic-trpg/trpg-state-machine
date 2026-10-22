"""Persistent metadata roots: balanced updates, isolation and bounded reachability."""

import random

import pytest

from trpg_state_machine import version_map
from trpg_state_machine.version_map import VersionMap


def audit(node, low=None, high=None):
    if node is None:
        return 0, 0
    assert low is None or low < node.order
    assert high is None or node.order < high
    lh, ls = audit(node.left, low, node.order)
    rh, rs = audit(node.right, node.order, high)
    assert abs(lh - rh) <= 1
    assert node.height == 1 + max(lh, rh)
    assert node.size == 1 + ls + rs
    return node.height, node.size


def test_versions_randomized_updates_deletes_and_frozen_roots():
    rng = random.Random(438)
    current, expected, snapshots = VersionMap(), {}, []
    keys = [(f"table{i % 13}", None if i % 7 == 0 else str(i)) for i in range(300)]
    for i in range(2400):
        key = rng.choice(keys)
        if i % 3 and key in expected:
            del current[key]
            del expected[key]
        else:
            current[key] = expected[key] = i
        audit(current._root)
        assert dict(current) == expected
        if i % 200 == 0:
            snapshots.append((current.fork(), dict(expected)))
    current.clear()
    assert len(current) == 0
    for view, values in snapshots:
        assert dict(view) == values
        audit(view._root)
    with pytest.raises(KeyError):
        del current[keys[0]]


def test_forks_share_roots_updates_copy_only_search_paths(monkeypatch):
    current = VersionMap(((f"key{i:05}", "s"), i) for i in range(4096))
    old = current.fork()
    built = []
    original = version_map._node

    def node(*args):
        value = original(*args)
        built.append(value)
        return value

    monkeypatch.setattr(version_map, "_node", node)
    copies = [current.fork() for _ in range(100)]
    assert all(view._root is current._root for view in copies)
    assert not built
    current[("key02000", "s")] = 9999
    assert 0 < len(built) <= 2 * old._root.height
    assert old[("key02000", "s")] == 2000
    assert current[("key02000", "s")] == 9999
    # A root reaches exactly one node per live key, never prior transaction roots.
    assert audit(current._root)[1] == len(current) == 4096
    assert current._root.size == old._root.size
