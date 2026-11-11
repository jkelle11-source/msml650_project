"""Lets the dashboard's tests run from AnomalyPulse/ or from dashboard/: live_source.py imports the incident
store's record module as `incidents.record`, as the Lambda bundle lays it out (dashboard/Makefile)."""
import sys
from pathlib import Path

ANOMALYPULSE = str(Path(__file__).resolve().parent.parent)
if ANOMALYPULSE not in sys.path:
    sys.path.insert(0, ANOMALYPULSE)
