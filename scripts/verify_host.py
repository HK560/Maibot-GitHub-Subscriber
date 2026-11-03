"""使用本机 MaiBot 的真实 ZIP 安装器在临时目录验证发布包。

使用安装版 MaiBot 的 Python 执行，并设置 MAIBOT_INSTANCE_DIR。
不修改运行实例的已安装插件或通知目标。
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json

from src.webui.routers.plugin import zip_install
from src.plugin_runtime.runner.plugin_loader import PluginLoader

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    bundle = ROOT / "dist" / f"mai-github-sub-v{manifest['version']}.zip"
    with TemporaryDirectory(prefix="github-sub-host-check-") as tmp:
        directory = Path(tmp)
        target = directory / "mai_github-sub"
        with patch.object(zip_install, "get_plugins_dir", return_value=directory), patch.object(
            zip_install, "get_plugin_candidate_paths", return_value=(target, directory / "old")
        ), patch.object(zip_install, "resolve_installed_plugin_path", return_value=None):
            with bundle.open("rb") as source:
                result = zip_install.install_zip_archive(source)
        assert result["success"], result
        assert not (target / "config.toml").exists()
        assert not (target / ".maibot-release.json").exists()
        loader = PluginLoader(host_version="1.3.19")
        loaded = loader.discover_and_load([str(directory)])
        assert not loader.failed_plugins, loader.failed_plugins
        meta = next((item for item in loaded if item.plugin_id == manifest["id"]), None)
        assert meta is not None
        plugin = meta.instance
        defaults = plugin.build_default_config()
        plugin.set_plugin_config(defaults)
        schema = plugin.get_webui_config_schema()
        assert len(schema["sections"]) == 8
        assert len(schema["runtime_widgets"]) == 5
        print("PASS: real MaiBot ZIP installer, manifest, plugin loader, SDK defaults and 8 config sections / 5 widgets")


if __name__ == "__main__":
    main()
