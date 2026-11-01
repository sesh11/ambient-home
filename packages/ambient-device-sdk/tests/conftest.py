"""Make module-level fake plugins importable by importlib.metadata.EntryPoint."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
