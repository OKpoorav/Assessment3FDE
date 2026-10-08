from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_ui_runs_rules_only_analysis_end_to_end():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    at.sidebar.radio[1].set_value("rules").run()  # architecture selector
    at.button[0].click().run()  # "Run analysis"
    assert not at.exception
    banner = " ".join(e.value for e in at.success) + " ".join(e.value for e in at.warning)
    assert "Route for business approval" in banner or "Review existing tool" in banner
    assert any("Human review" in s.value for s in at.subheader)
