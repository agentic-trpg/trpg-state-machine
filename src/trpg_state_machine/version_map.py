"""Persistent AVL metadata for the existing UnitOfWork, never domain state.

Fork/checkpoint/publication share immutable roots in O(1). Updating one version
copies only its O(log N) search path. There is no chain of transaction overlays;
unreferenced roots/nodes are reclaimed normally. Whole enumeration is explicit.
"""

from collections.abc import MutableMapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Node:
    key: tuple
    value: int
    order: tuple
    left: object
    right: object
    height: int
    size: int


def _height(node):
    return node.height if node else 0


def _node(key, value, left=None, right=None):
    return _Node(
        key,
        value,
        (key[0], key[1] is not None, key[1] or ""),
        left,
        right,
        1 + max(_height(left), _height(right)),
        1 + (left.size if left else 0) + (right.size if right else 0),
    )


def _balance(key, value, left, right):
    if _height(left) - _height(right) > 1:
        if _height(left.left) < _height(left.right):
            pivot = left.right
            left = _node(
                pivot.key,
                pivot.value,
                _node(left.key, left.value, left.left, pivot.left),
                pivot.right,
            )
        return _node(left.key, left.value, left.left, _node(key, value, left.right, right))
    if _height(right) - _height(left) > 1:
        if _height(right.right) < _height(right.left):
            pivot = right.left
            right = _node(
                pivot.key,
                pivot.value,
                pivot.left,
                _node(right.key, right.value, pivot.right, right.right),
            )
        return _node(right.key, right.value, _node(key, value, left, right.left), right.right)
    return _node(key, value, left, right)


def _set(root, key, order, value):
    if root is None:
        return _node(key, value)
    if order == root.order:
        return root if value == root.value else _node(key, value, root.left, root.right)
    if order < root.order:
        left = _set(root.left, key, order, value)
        return root if left is root.left else _balance(root.key, root.value, left, root.right)
    right = _set(root.right, key, order, value)
    return root if right is root.right else _balance(root.key, root.value, root.left, right)


def _delete(root, key, order):
    if root is None:
        raise KeyError(key)
    if order < root.order:
        return _balance(root.key, root.value, _delete(root.left, key, order), root.right)
    if order > root.order:
        return _balance(root.key, root.value, root.left, _delete(root.right, key, order))
    if root.left is None:
        return root.right
    if root.right is None:
        return root.left
    successor = root.right
    while successor.left is not None:
        successor = successor.left
    return _balance(
        successor.key,
        successor.value,
        root.left,
        _delete(root.right, successor.key, successor.order),
    )


class VersionMap(MutableMapping):
    """A private mutable root handle; forks share only frozen nodes."""

    def __init__(self, entries=(), *, root=None):
        self._root = root
        for key, value in entries:
            self[key] = value

    def fork(self):
        return type(self)(root=self._root)

    def __getitem__(self, key):
        order, node = (key[0], key[1] is not None, key[1] or ""), self._root
        while node is not None:
            if order == node.order:
                return node.value
            node = node.left if order < node.order else node.right
        raise KeyError(key)

    def __setitem__(self, key, value):
        self._root = _set(self._root, key, (key[0], key[1] is not None, key[1] or ""), value)

    def __delitem__(self, key):
        self._root = _delete(self._root, key, (key[0], key[1] is not None, key[1] or ""))

    def __iter__(self):
        node, stack = self._root, []
        while node is not None or stack:
            while node is not None:
                stack.append(node)
                node = node.left
            node = stack.pop()
            yield node.key
            node = node.right

    def __len__(self):
        return self._root.size if self._root else 0

    def clear(self):
        self._root = None
