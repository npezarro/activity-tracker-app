import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
_tmp = tempfile.mkdtemp(prefix="at-tests-")
os.environ["ACTIVITYTRACKER_DATA_DIR"] = _tmp
os.environ["ACTIVITYTRACKER_DB"] = os.path.join(_tmp, "test.db")
