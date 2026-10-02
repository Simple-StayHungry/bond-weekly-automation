"""确保 app/core 在 sys.path（main_mac 使用顶层 import rules），
任何 app.web.* 被直接导入时 Core 也能正常加载。"""
import os
import sys

_CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
