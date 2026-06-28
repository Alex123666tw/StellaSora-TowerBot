"""gui/profiles.py 設定檔多 profile 純函數測試（GUI_DESIGN_SPEC §10 步5）。

profile 存 configs/<名稱>.yaml;config.yaml 是「當前生效」設定。
載入 profile = 把該 yaml 覆寫進 config.yaml。
"""
from __future__ import annotations

import pytest
import yaml

from gui import profiles


@pytest.fixture
def tmp_profiles(tmp_path, monkeypatch):
    pdir = tmp_path / "configs"
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(profiles, "_PROFILES_DIR", pdir)
    monkeypatch.setattr(profiles, "_CONFIG_PATH", cfg)
    return pdir, cfg


class TestValidName:
    def test_valid(self):
        assert profiles.is_valid_name("預設")
        assert profiles.is_valid_name("aggressive-1")
        assert profiles.is_valid_name("保守 省錢流")

    def test_invalid(self):
        assert not profiles.is_valid_name("")
        assert not profiles.is_valid_name("a/b")        # 路徑分隔字元
        assert not profiles.is_valid_name("../evil")    # 路徑跳脫
        assert not profiles.is_valid_name("x" * 50)     # 過長


class TestSaveLoadList:
    def test_save_then_load_roundtrip(self, tmp_profiles):
        cfg = {"decision": {"mode": "legacy"}, "run": {"max_runs": 3}}
        profiles.save_profile("衝分流", cfg)
        assert profiles.load_profile("衝分流") == cfg

    def test_list_profiles_sorted(self, tmp_profiles):
        assert profiles.list_profiles() == []
        profiles.save_profile("B套組", {"y": 2})
        profiles.save_profile("A套組", {"x": 1})
        assert profiles.list_profiles() == ["A套組", "B套組"]

    def test_delete(self, tmp_profiles):
        profiles.save_profile("tmp", {"x": 1})
        assert profiles.delete_profile("tmp") is True
        assert profiles.list_profiles() == []
        assert profiles.delete_profile("nonexistent") is False

    def test_save_invalid_name_raises(self, tmp_profiles):
        with pytest.raises(ValueError):
            profiles.save_profile("a/b", {})

    def test_load_missing_raises(self, tmp_profiles):
        with pytest.raises(FileNotFoundError):
            profiles.load_profile("nope")


class TestApplyImportExport:
    def test_apply_overwrites_config(self, tmp_profiles):
        _pdir, cfgpath = tmp_profiles
        profiles.save_profile("衝分流", {"run": {"max_runs": 5}})
        applied = profiles.apply_profile_to_config("衝分流")
        assert applied == {"run": {"max_runs": 5}}
        with open(cfgpath, "r", encoding="utf-8") as f:
            assert yaml.safe_load(f) == {"run": {"max_runs": 5}}

    def test_import_then_export_roundtrip(self, tmp_profiles, tmp_path):
        src = tmp_path / "external.yaml"
        with open(src, "w", encoding="utf-8") as f:
            yaml.dump({"a": 1}, f, allow_unicode=True)
        profiles.import_profile(str(src), "imported")
        assert profiles.load_profile("imported") == {"a": 1}
        dst = tmp_path / "out.yaml"
        profiles.export_profile("imported", str(dst))
        with open(dst, "r", encoding="utf-8") as f:
            assert yaml.safe_load(f) == {"a": 1}
