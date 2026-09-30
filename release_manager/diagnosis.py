"""Rule-based, plain-language diagnosis for failed Test Result errors.

Turns a raw Cypress/engine error string into a short "what likely happened +
what to check" draft so the Corrective Action field isn't blank for a
functional tester to start from cold. Deliberately not a verdict — it's a
starting point the tester reviews and edits, hence the "Suggested" prefix.
"""

from __future__ import annotations

import re

_DRAFT_PREFIX = "Suggested (auto-diagnosed — please review):\n\n"

# Ordered: first pattern to match wins, so put more specific signatures
# (e.g. a numbered HTTP status) before the generic ones they might overlap.
_RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "selector-timeout",
        re.compile(r"Timed out retrying.*Expected to find element", re.I | re.S),
        "The page never showed the element the test needed (see the selector "
        "in the error above). Likely causes: the field was renamed, hidden "
        "by a Property Setter or permission, or the page was still loading "
        "when the check ran. Check: open the same screen manually on this "
        "site and confirm the field is visible; check Customisations "
        "(Custom Field / Property Setter) for that fieldname.",
    ),
    (
        "permission",
        re.compile(r"\b(403|PermissionError|not permitted|insufficient permission)\b", re.I),
        "The test user was blocked by a permission or role check. Check: "
        "the API user/role used for this Testing Site still has the roles "
        "this test needs, and that no recent Role Permission or DocType "
        "Permission change removed access.",
    ),
    (
        "auth",
        re.compile(r"\b(401|invalid credentials|authentication failed|session expired)\b", re.I),
        "The test could not log in or its session was rejected. Check: the "
        "API key/secret or password stored on this Testing Site is still "
        "valid, and the account isn't locked or disabled on the target.",
    ),
    (
        "not-found",
        re.compile(r"\b(404|does not exist|DoesNotExistError)\b", re.I),
        "The test looked for a record, DocType, or route that wasn't there. "
        "Check: the required app is installed on this site, and that any "
        "fixture data the test depends on exists (it may have been deleted "
        "or renamed).",
    ),
    (
        "server-error",
        re.compile(r"(500 |Internal Server Error|Traceback \(most recent call last\))", re.I),
        "The target site raised a server-side error while the test ran. "
        "Check: the Error Log on the target site around this run's time for "
        "the full traceback — this is usually a real application bug, not a "
        "test problem.",
    ),
    (
        "network",
        re.compile(r"(ECONNREFUSED|ENOTFOUND|getaddrinfo|net::ERR_|could not verify)", re.I),
        "The test couldn't reach the target site over the network. Check: "
        "the site's base URL/Host header on the Testing Site record, that "
        "the site is up, and that this worker can reach it.",
    ),
    (
        "stale-element",
        re.compile(r"(detached from the DOM|stale element)", re.I),
        "The page changed under the test mid-step (likely a re-render or "
        "redirect). This is often flaky rather than a real bug. Check: "
        "re-run once before investigating further.",
    ),
    (
        "assertion",
        re.compile(r"AssertionError:.*expected", re.I | re.S),
        "The test ran fine but got a different value than it expected. "
        "Check: whether a recent app change intentionally altered this "
        "behaviour (update the test) or it's a regression (file a bug); "
        "also check the test's fixture/seed data still matches what the "
        "assertion expects.",
    ),
]


def diagnose(error: str | None) -> str | None:
    """Return a plain-language draft for a failed Test Result's error, or None.

    None means no rule matched — callers should leave ``corrective_action``
    blank rather than write a useless "review manually" placeholder.
    """
    if not error:
        return None
    for _name, pattern, explanation in _RULES:
        if pattern.search(error):
            return _DRAFT_PREFIX + explanation
    return None
