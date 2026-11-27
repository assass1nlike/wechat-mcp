import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def load() -> dict:
    path = ROOT / "config.toml"
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path}，照 config.example.toml 写一份")
    return tomllib.loads(path.read_text(encoding="utf-8"))
