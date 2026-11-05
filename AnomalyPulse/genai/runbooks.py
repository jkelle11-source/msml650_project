"""Runbooks for the diagnosis (M6-2): one per incident type, picked by the classified type.

    runbook_for("DATABASE_THROTTLING") -> ("database_throttling", "<the runbook's text>")

With five types the pick is a lookup, so there is no vector store: a runbook is
the wrong one exactly when the classification is, and "retrieval quality" is
the classifier's accuracy (M9-2). The runbooks feed recommended_actions only
(prompt.user_message), behind ANOMALYPULSE_RUNBOOKS, which is off by default.

The texts are written for an on-call engineer on this stack, with its real
numbers (Handoffs/M6-1_Handoff.md, section 4): every number in a runbook becomes
one the self-check allows in recommended_actions, so a wrong one would reach
every diagnosis.
"""
from pathlib import Path

from genai.contract import INCIDENT_TYPES

RUNBOOK_DIR = Path(__file__).parent / "runbooks"


def runbook_for(incident_type: str) -> tuple[str, str]:
    """(name, text) of the runbook for `incident_type`. Raises rather than return an empty one."""
    if incident_type not in INCIDENT_TYPES:
        raise ValueError(f"no runbook for incident type {incident_type!r}")
    name = incident_type.lower()
    text = (RUNBOOK_DIR / f"{name}.md").read_text().strip()   # FileNotFoundError if it's missing
    if not text:
        raise ValueError(f"runbook {name}.md is empty")
    return name, text
