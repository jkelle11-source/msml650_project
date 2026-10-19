"""features/ is a package, so its tests import it by name from the directory above it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
