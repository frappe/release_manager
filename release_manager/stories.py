"""User Story import and export.

Stories are drafted by the agent skills (from support tickets or a release
changelog), reviewed by the support team in Desk, and only Approved stories
are turned into tests. This module is the door in and out:

- ``import_stories`` creates or refreshes Draft stories, keyed by ``story_key``.
  A story someone has already reviewed is never overwritten.
- ``approved_stories`` returns the approved set, for CI to pin into the repo.
"""

from __future__ import annotations

import json

import frappe

_IMPORTABLE = (
    "title",
    "app",
    "product_area",
    "risk",
    "persona",
    "layer",
    "story",
    "steps",
    "expected_results",
    "source_type",
    "ticket_count",
    "source_tickets",
    "source_prs",
)

_EXPORTED = (
    "name",
    "story_key",
    "modified",
    *_IMPORTABLE,
    "check_list",
    "test_reference",
    "test_status",
)


@frappe.whitelist()
def import_stories(stories: str | list) -> dict:
    """Create new Draft stories and refresh Drafts with the same ``story_key``.

    Returns counts: created, updated, kept (already reviewed, left alone).
    """
    frappe.only_for("System Manager")
    if isinstance(stories, str):
        stories = json.loads(stories)
    counts = {"created": 0, "updated": 0, "kept": 0}
    for row in stories:
        key = row.get("story_key")
        if not key:
            frappe.throw("Every imported story needs a story_key")
        values = {k: row[k] for k in _IMPORTABLE if k in row}
        name = frappe.db.get_value("User Story", {"story_key": key})
        if not name:
            frappe.get_doc(
                {"doctype": "User Story", "story_key": key, "status": "Draft", **values}
            ).insert()
            counts["created"] += 1
            continue
        doc = frappe.get_doc("User Story", name)
        if doc.status != "Draft":
            counts["kept"] += 1
            continue
        doc.update(values)
        doc.save()
        counts["updated"] += 1
    frappe.db.commit()
    return counts


@frappe.whitelist()
def approved_stories() -> list[dict]:
    """Every Approved story, oldest first, in the shape CI pins into the repo."""
    return frappe.get_all(
        "User Story",
        filters={"status": "Approved"},
        fields=list(_EXPORTED),
        order_by="creation asc",
    )
