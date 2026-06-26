import sys
from pathlib import Path

# Put the sync/ package root on the path so `import config`, `from resolvers ...`
# resolve the same way they do when sync.py runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
