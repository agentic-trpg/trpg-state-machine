"""Closed portable-unit metadata, shared by Package admission and current facts."""

import hashlib
import re


def core_item_id(sid, owner, position):
    """Give one explicit core inventory occurrence a stable object identity."""
    digest = hashlib.sha256(f"{sid}\0{owner}".encode()).hexdigest()[:20]
    return f"legacy.inventory.{digest}.{position:08d}"


def valid_item_unit(data):
    """Check the item record's closed fields, identity, quantity and charge constraints."""
    return (
        type(data) is dict
        and set(data) == {"item_slug", "quantity", "charges_remaining_per_unit", "accessible"}
        and type(data["item_slug"]) is str
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", data["item_slug"]) is not None
        and type(data["quantity"]) is int
        and data["quantity"] == 1
        and type(data["charges_remaining_per_unit"]) is int
        and 0 <= data["charges_remaining_per_unit"] <= 2**63 - 1
        and type(data["accessible"]) is bool
    )


def item_unit_from_row(row):
    """Rebuild the fixed mechanical unit DTO from Schema49 scalar item facts."""
    if row["item_slug"] is None:
        return None
    return {
        "item_slug": row["item_slug"],
        "quantity": 1,
        "charges_remaining_per_unit": row["item_charges"],
        "accessible": bool(row["item_accessible"]),
    }
