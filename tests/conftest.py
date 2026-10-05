import sys
from pathlib import Path

# 测试只覆盖不依赖 AstrBot 的 remoteci 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
