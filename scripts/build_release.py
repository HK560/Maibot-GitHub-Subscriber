"""构建可供 MaiBot WebUI 上传的 ZIP，不打包任何本地配置、凭据或收据。"""
from pathlib import Path
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "_manifest.json", "__init__.py", "plugin.py", "config.py", "client.py",
    "engine.py", "notify.py", "state.py", "models.py", "config.example.toml",
    "requirements.txt", "pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE",
)


def build() -> Path:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    destination = ROOT / "dist" / f"maibot-github-subscriber-v{manifest['version']}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"缺少发布文件或文件是链接：{name}")
            archive.write(path, arcname=name)
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(FILES)
    print(f"{destination} ({destination.stat().st_size} bytes)")
    return destination


if __name__ == "__main__":
    build()
