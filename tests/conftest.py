import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# keep test runs away from real data: the engine and the SaaS both get throwaway roots
os.environ.setdefault("SAAS_DATA_DIR", tempfile.mkdtemp(prefix="virlo-test-"))
os.environ.setdefault("CLIPPER_ROOT", tempfile.mkdtemp(prefix="clipper-test-"))
