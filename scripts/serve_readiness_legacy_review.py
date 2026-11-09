"""Private Streamlit harness using the real old renderer and immutable real saved outputs."""
import hashlib
import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from schema.analysis import AnalysisResult
from streamlit_ui.analysis_renderer import render_analysis_result

OUT = ROOT / "var/validation/20261108-release-readiness"
gbar = json.loads((OUT / "private/saved-gbar-output.json").read_text())
uav_rows = json.loads((ROOT / "var/validation/20261108-business-parity/private/uav_plot.json").read_text())["receipts"]
uav = next(r for r in uav_rows if r["operation"] == "get_uav_samples")
options = {"GBAR真实已保存输出": (gbar["analysis_result"], gbar["payload_sha256"]), "UAV真实已保存样本": (uav["analysis_result"], uav["payload_sha256"])}
st.set_page_config(page_title="真实同数据旧渲染器验收", layout="wide")
st.title("真实同数据旧渲染器验收")
st.caption("独立只读渲染器验收入口；不能替代旧业务站完整操作链路对等验收。")
choice = st.selectbox("真实制品", list(options))
value, sha = options[choice]
st.code("SOURCE_SHA256=" + sha, language=None)
st.caption("RENDERER_SHA256=" + hashlib.sha256((ROOT / "src/streamlit_ui/analysis_renderer.py").read_bytes()).hexdigest())
result = AnalysisResult.model_validate(value)
render_analysis_result(result, widget_prefix="readiness_real")
st.caption("BLOCK_TYPES=" + ",".join(sorted({b.type for b in result.blocks})))
