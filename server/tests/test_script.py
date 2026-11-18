import json
import shutil
import subprocess
from pathlib import Path

import pytest

from prodbat_server.app import SCRIPT

HARNESS = Path(__file__).parent / "run_script.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_script_reports_page_views_errors_and_failed_requests():
    out = subprocess.run(["node", str(HARNESS), str(SCRIPT)], capture_output=True, text=True, check=True)
    result = json.loads(out.stdout)
    sent = result["sent"]
    assert len(result["ref"]) >= 6
    assert result["link"] == f"https://buy.stripe.com/abc?client_reference_id={result['ref']}"
    assert {e["i"] for e in sent if e["t"] == "pv"} == {result["ref"]}

    # Everything goes to /api/e on the origin that served the script.
    assert {e["url"] for e in sent} == {"https://cdn.example/api/e"}
    assert {e["k"] for e in sent} == {"pb_test"}

    views = [e for e in sent if e["t"] == "pv"]
    assert [(e["u"], e["r"]) for e in views] == [
        ("https://yourapp.com/pricing?utm_source=x", "https://chatgpt.com/"),
        ("https://yourapp.com/signup", "https://yourapp.com/pricing"),
    ]

    errors = [e for e in sent if e["t"] == "err"]
    assert [(e["m"], e["l"]) for e in errors] == [("x is not defined", 12), ("nope", 0)]

    requests = [e for e in sent if e["t"] == "req"]
    assert [(e["me"], e["ru"], e["st"]) for e in requests] == [
        ("POST", "/api/boom", 500),
        ("GET", "/api/offline", 0),
    ]
