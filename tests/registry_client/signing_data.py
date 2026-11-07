"""The published SigV4 test-suite vectors and the botocore-generated ECR oracle."""

import json
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent
SUITE: list[dict[str, Any]] = json.loads((DATA / "sigv4_suite_vectors.json").read_text("utf-8"))
ORACLE: dict[str, Any] = json.loads((DATA / "sigv4_botocore_oracle.json").read_text("utf-8"))
