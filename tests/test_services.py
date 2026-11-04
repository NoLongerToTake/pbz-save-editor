"""Service contracts, exercised with real files; never use live game saves."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


from pbz_editor import services


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pbz-services-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, relative, data=b"old save"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class ManualBackupTests(FileTests):
    def test_manual_backup_is_exact_unique_and_does_not_replace_source(self):
        source = self.put("自选/原档.sav", b"unaltered snapshot")
        before = source.stat().st_mtime_ns
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        first = services.create_backup(source, self.root / "备份", digest)
        second = services.create_backup(source, self.root / "备份", digest)
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        self.assertEqual(first["backup_path"].read_bytes(), source.read_bytes())
        self.assertEqual(first["source_sha256"], digest)
        self.assertEqual(source.stat().st_mtime_ns, before)

    def test_manual_backup_rejects_external_change(self):
        source = self.put("原档.sav", b"changed externally")
        with self.assertRaises(ValueError):
            services.create_backup(source, self.root / "备份", "0" * 64)
        self.assertFalse((self.root / "备份").exists())


class SettingsTests(FileTests):
    def test_missing_settings_load_without_creating_directory(self):
        base = self.root / "settings"
        self.assertEqual(services.SettingsStore(base).load(), {})
        self.assertFalse(base.exists())

    def test_remembers_custom_save_and_backup_paths_across_instances(self):
        base = self.root / "配置"
        values = {"save_path": str(self.root / "自选" / "PBZSaveGame.sav"),
                  "backup_dir": str(self.root / "备份"), "other": {"slot": 3}}
        services.SettingsStore(base).save(values)
        self.assertEqual(services.SettingsStore(base).load(), values)

    def test_settings_accept_path_values(self):
        store = services.SettingsStore(self.root)
        store.save({"backup_dir": self.root / "backups"})
        self.assertEqual(store.load()["backup_dir"], str(self.root / "backups"))

    def test_malformed_or_non_object_settings_raise(self):
        store = services.SettingsStore(self.root)
        for raw in (b"not json", b"[]"):
            with self.subTest(raw=raw):
                self.put("settings.json", raw)
                with self.assertRaises(ValueError):
                    store.load()

    def test_save_rejects_non_dict_without_overwriting(self):
        store = services.SettingsStore(self.root)
        store.save({"keep": True})
        with self.assertRaises(TypeError):
            store.save([1, 2])
        self.assertEqual(store.load(), {"keep": True})

    def test_failed_atomic_settings_write_preserves_previous_and_user_files(self):
        store = services.SettingsStore(self.root)
        store.save({"keep": True})
        user_temp = self.put("user.tmp", b"user data")
        with patch.object(services.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                store.save({"keep": False})
        self.assertEqual(store.load(), {"keep": True})
        self.assertEqual(user_temp.read_bytes(), b"user data")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"settings.json", "user.tmp"})

    def test_default_directories_use_user_data_and_not_current_working_directory(self):
        local = self.root / "user-data"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            store = services.SettingsStore()
            backup = services.default_backup_dir()
            self.assertTrue(backup.is_relative_to(local))
            self.assertFalse(backup.exists())
            store.save({"backup_dir": str(backup)})
            self.assertEqual(services.SettingsStore().load(), {"backup_dir": str(backup)})
            self.assertEqual(len(list(local.rglob("settings.json"))), 1)

    def test_portable_root_keeps_settings_and_backups_together(self):
        portable = self.root / "portable"
        store = services.SettingsStore(base_dir=portable)
        backup = services.default_backup_dir(base_dir=portable)
        self.assertEqual(backup, portable / "backups")
        store.save({"backup_dir": backup, "save_path": self.root / "manual.sav"})
        self.assertEqual(services.SettingsStore(portable).load()["backup_dir"], str(backup))
        self.assertEqual({p.name for p in portable.iterdir()}, {"settings.json"})


class DiscoveryTests(FileTests):
    def setUp(self):
        super().setUp()
        self.local = self.root / "LocalAppData"
        self.steam = self.root / "Steam"
        self.drives = self.root / "disk"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "ProgramFiles": "",
                               "ProgramFiles(x86)": "", "STEAM_PATH": ""}).start()
        patch.object(services, "_registry_steam_dirs", return_value=[self.steam]).start()
        patch.object(services, "_drive_roots", return_value=[self.drives]).start()

    def steam_save(self, library, account="76561198000000001"):
        return self.put(library / "steamapps/common/PBZ/ProjectPBZ/Saved/SaveGames/Steam"
                        / account / "PBZSaveGame.sav")

    def test_registry_install_modern_vdf_legacy_vdf_and_drive_library(self):
        main = self.steam_save(self.steam)
        modern_root = self.root / "external-library"
        legacy_root = self.root / "legacy-library"
        modern = self.steam_save(modern_root, "111")
        legacy = self.steam_save(legacy_root, "222")
        drive = self.steam_save(self.drives / "SteamLibrary", "333")
        # Valve's real escaping, comments, nested apps, and both VDF generations.
        escaped = str(modern_root).replace("\\", "\\\\")
        self.put(self.steam / "steamapps/libraryfolders.vdf",
                 ('"libraryfolders" { // installed libraries\n'
                  '"0" { "path" "' + escaped + '" "apps" { "123" "456" } } }').encode())
        escaped = str(legacy_root).replace("\\", "\\\\")
        self.put(self.steam / "config/libraryfolders.vdf",
                 ('"LibraryFolders" { "1" "' + escaped + '" }').encode())
        self.assertEqual(set(services.discover_saves()), {main, modern, legacy, drive})

    def test_localappdata_known_layouts_and_multiple_accounts(self):
        direct = self.put(self.local / "ProjectPBZ/Saved/SaveGames/PBZSaveGame.sav")
        steam = self.put(self.local / "ProjectPBZ/Saved/SaveGames/Steam/111/PBZSaveGame.sav")
        account = self.put(self.local / "ProjectPBZ/Saved/SaveGames/222/PBZSaveGame.sav")
        self.assertEqual(set(services.discover_saves()), {direct, steam, account})

    def test_ignores_project_copies_backups_and_unrelated_nested_files(self):
        good = self.steam_save(self.steam)
        self.put("正存档/PBZSaveGame.sav")
        self.put("content/output/PBZSaveGame.sav")
        self.put(self.steam / "backups/PBZSaveGame.sav")
        self.put(good.parent / "PBZSaveGame.sav.bak")
        self.put(good.parent / "backup/PBZSaveGame.sav")
        self.assertEqual(services.discover_saves(), [good])

    def test_duplicate_library_paths_return_each_save_once(self):
        good = self.steam_save(self.steam)
        with patch.object(services, "_registry_steam_dirs", return_value=[self.steam, self.steam]):
            self.assertEqual(services.discover_saves(), [good])

    def test_program_files_finds_steam_without_registry(self):
        program_files = self.root / "Programs"
        good = self.steam_save(program_files / "Steam")
        with patch.object(services, "_registry_steam_dirs", return_value=[]), \
                patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}):
            self.assertEqual(services.discover_saves(), [good])

    def test_empty_machine_returns_no_candidates(self):
        self.assertEqual(services.discover_saves(), [])

    def test_not_ready_drive_does_not_hide_working_library(self):
        good = self.steam_save(self.steam)
        real_stat = Path.stat

        def not_ready(path, *args, **kwargs):
            if path.is_relative_to(self.drives):
                error = PermissionError("device is not ready")
                error.winerror = 21
                raise error
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", not_ready):
            self.assertEqual(services.discover_saves(), [good])

    def test_real_access_denial_is_reported(self):
        self.steam_save(self.steam)
        real_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path.is_relative_to(self.steam):
                error = PermissionError("access denied")
                error.winerror = 5
                raise error
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", denied):
            with self.assertRaises(PermissionError):
                services.discover_saves()

    def test_bad_vdf_reports_error_instead_of_silently_hiding_library(self):
        self.put(self.steam / "steamapps/libraryfolders.vdf", b'"libraryfolders" {')
        with self.assertRaises(ValueError):
            services.discover_saves()


class BackupWriteTests(FileTests):
    def setUp(self):
        super().setUp()
        self.source = self.put("game/PBZSaveGame.sav", b"original source")
        self.backups = self.root / "backups"
        self.sha = hashlib.sha256(b"original source").hexdigest()

    def write(self, **kwargs):
        return services.write_with_backup(self.source, b"edited save", self.backups, **kwargs)

    def test_overwrite_creates_verified_backup_and_reports_hashes(self):
        result = self.write(expected_sha256=self.sha)
        self.assertEqual(result["backup_path"].read_bytes(), b"original source")
        self.assertEqual(result["backup_path"].suffix, ".bak")
        self.assertEqual(result["backup_path"].parent, self.backups)
        self.assertEqual(result["output_path"], self.source)
        self.assertEqual(self.source.read_bytes(), b"edited save")
        self.assertEqual(result["source_sha256"], self.sha)
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"edited save").hexdigest())

    def test_save_as_new_keeps_source_and_backs_it_up(self):
        target = self.root / "export/PBZSaveGame.sav"
        result = self.write(destination=target)
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual(target.read_bytes(), b"edited save")
        self.assertEqual(result["backup_path"].read_bytes(), b"original source")

    def test_save_as_existing_backs_up_both_files_before_overwrite(self):
        target = self.put("export/PBZSaveGame.sav", b"previous export")
        result = self.write(destination=target)
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual(target.read_bytes(), b"edited save")
        self.assertEqual(result["destination_backup_path"].read_bytes(), b"previous export")
        self.assertEqual({p.read_bytes() for p in self.backups.glob("*.bak")},
                         {b"original source", b"previous export"})

    def test_stale_expected_sha_aborts_before_creating_backup_or_output(self):
        self.source.write_bytes(b"changed externally")
        target = self.root / "new.sav"
        with self.assertRaises(ValueError):
            self.write(destination=target, expected_sha256=self.sha)
        self.assertEqual(self.source.read_bytes(), b"changed externally")
        self.assertFalse(self.backups.exists())
        self.assertFalse(target.exists())

    def test_backup_directory_failure_keeps_source_and_target(self):
        self.backups.write_bytes(b"not a directory")
        target = self.put("out.sav", b"existing target")
        with self.assertRaises(OSError):
            self.write(destination=target)
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual(target.read_bytes(), b"existing target")

    def test_corrupted_backup_readback_aborts(self):
        read_bytes = Path.read_bytes

        def corrupted(path):
            data = read_bytes(path)
            return b"corrupt" if path.suffix == ".bak" else data

        with patch.object(Path, "read_bytes", corrupted):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(self.source.read_bytes(), b"original source")

    def test_second_backup_failure_does_not_overwrite_save_as_target(self):
        target = self.put("out.sav", b"existing target")
        read_bytes = Path.read_bytes

        def corrupted(path):
            data = read_bytes(path)
            return b"corrupt" if path.suffix == ".bak" and path.name.startswith("out.sav.") else data

        with patch.object(Path, "read_bytes", corrupted):
            with self.assertRaises(OSError):
                self.write(destination=target)
        self.assertEqual(target.read_bytes(), b"existing target")
        self.assertEqual(self.source.read_bytes(), b"original source")

    def test_replace_failure_keeps_original_backup_and_user_temp(self):
        user_temp = self.put("game/user.tmp", b"keep me")
        with patch.object(services.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.write()
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual(user_temp.read_bytes(), b"keep me")
        self.assertEqual(len(list(self.backups.glob("*.bak"))), 1)
        self.assertEqual({p.name for p in self.source.parent.iterdir()},
                         {"PBZSaveGame.sav", "user.tmp"})

    def test_fsync_failure_aborts_before_overwrite(self):
        with patch.object(services.os, "fsync", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(self.source.read_bytes(), b"original source")

    def test_staged_file_fsync_failure_keeps_source_and_verified_backup(self):
        real_fsync = services.os.fsync
        calls = 0

        def fail_staging(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("staging disk error")
            return real_fsync(fd)

        with patch.object(services.os, "fsync", fail_staging):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual([p.read_bytes() for p in self.backups.glob("*.bak")],
                         [b"original source"])
        self.assertEqual(list(self.source.parent.iterdir()), [self.source])

    def test_staged_file_readback_corruption_keeps_existing_save_as_target(self):
        target = self.put("out.sav", b"existing target")
        read_bytes = Path.read_bytes

        def corrupted(path):
            data = read_bytes(path)
            return b"bad staged data" if path.suffix == ".tmp" else data

        with patch.object(Path, "read_bytes", corrupted):
            with self.assertRaises(OSError):
                self.write(destination=target)
        self.assertEqual(self.source.read_bytes(), b"original source")
        self.assertEqual(target.read_bytes(), b"existing target")
        self.assertEqual(len(list(self.backups.glob("*.bak"))), 2)

    def test_backups_are_unique_and_old_user_backups_survive(self):
        old = self.put("backups/keep.bak", b"user backup")
        first = self.write()["backup_path"]
        second = self.write()["backup_path"]
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"original source")
        self.assertEqual(second.read_bytes(), b"edited save")
        self.assertEqual(old.read_bytes(), b"user backup")

    def test_missing_source_never_creates_output(self):
        target = self.root / "out.sav"
        with self.assertRaises(FileNotFoundError):
            services.write_with_backup(self.root / "missing.sav", b"data", self.backups,
                                       destination=target)
        self.assertFalse(target.exists())

    def test_source_changes_during_backup_abort_without_overwriting_external_change(self):
        read_bytes = Path.read_bytes

        def concurrent_change(path):
            data = read_bytes(path)
            if path.suffix == ".bak":
                self.source.write_bytes(b"external new save")
            return data

        with patch.object(Path, "read_bytes", concurrent_change):
            with self.assertRaises(ValueError):
                self.write(expected_sha256=self.sha)
        self.assertEqual(self.source.read_bytes(), b"external new save")

    def test_target_appears_during_backup_abort_without_overwriting_it(self):
        target = self.root / "out.sav"
        read_bytes = Path.read_bytes

        def concurrent_create(path):
            data = read_bytes(path)
            if path.suffix == ".bak":
                target.write_bytes(b"external target")
            return data

        with patch.object(Path, "read_bytes", concurrent_create):
            with self.assertRaises(FileExistsError):
                self.write(destination=target)
        self.assertEqual(target.read_bytes(), b"external target")

    def test_existing_target_changes_during_backup_are_preserved(self):
        target = self.put("out.sav", b"existing target")
        read_bytes = Path.read_bytes

        def concurrent_change(path):
            data = read_bytes(path)
            if path.suffix == ".bak":
                target.write_bytes(b"external target update")
            return data

        with patch.object(Path, "read_bytes", concurrent_change):
            with self.assertRaises(ValueError):
                self.write(destination=target)
        self.assertEqual(target.read_bytes(), b"external target update")
        self.assertEqual(self.source.read_bytes(), b"original source")

    def test_non_bytes_data_rejected_before_any_changes(self):
        with self.assertRaises(TypeError):
            services.write_with_backup(self.source, "text", self.backups)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.source.read_bytes(), b"original source")


if __name__ == "__main__":
    unittest.main()
