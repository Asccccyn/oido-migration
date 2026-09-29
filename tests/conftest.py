import sys
from pathlib import Path

# src 布局：未安装时可编辑导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
