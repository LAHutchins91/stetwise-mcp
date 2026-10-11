"""Extension points that v1 deliberately does not implement.

PowerPoint, LibreOffice recalculation, Drive/OneDrive, and multi-file batches
belong in later stages. Callers get a refusal from detect() / FUTURE instead of
a guessed edit.
"""

from .engine import FUTURE

PPTX = "pptx"
RECALC = "recalc"
CLOUD_FILES = "cloud"
BATCH = "batch"


def unavailable(feature: str) -> str:
    return FUTURE[feature]
