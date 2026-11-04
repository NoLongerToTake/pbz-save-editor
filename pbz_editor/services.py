"""Bounded save discovery, per-user preferences, and backup-first atomic writes.

Discovery never searches the working directory or recursively scans a disk.
All mutating operations propagate errors. Only temporary files created by this
module are cleaned up; existing backups and user files are never pruned.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable
from uuid import uuid4


SAVE_NAME = "PBZSaveGame.sav"


def _is_file(path: Path) -> bool:
    # Path.is_file suppresses some I/O errors on recent Python versions.
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        if getattr(error, "winerror", None) in (21, 1112):
            return False  # Empty/unready removable device, not an ACL denial.
        raise


def _unique_paths(paths) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        resolved = Path(path).expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _registry_steam_dirs() -> list[Path]:
    """Read Steam's user key and both machine registry views, without writes."""
    if os.name != "nt":
        return []
    import winreg

    roots = []
    entries = [(winreg.HKEY_CURRENT_USER, "SteamPath", 0),
               (winreg.HKEY_CURRENT_USER, "SteamExe", 0)]
    entries.extend((winreg.HKEY_LOCAL_MACHINE, "InstallPath", view)
                   for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY))
    for hive, name, view in entries:
        try:
            with winreg.OpenKey(hive, r"Software\Valve\Steam", 0,
                                winreg.KEY_READ | view) as key:
                value, value_type = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            continue  # An uninstalled Steam or absent registry view is normal.
        if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value:
            path = Path(os.path.expandvars(value))
            roots.append(path.parent if name == "SteamExe" else path)
    return _unique_paths(roots)


def _drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDrives.restype = ctypes.c_uint32
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = kernel32.GetLogicalDrives()
    if not mask:
        raise ctypes.WinError(ctypes.get_last_error())
    # Local disks only: don't wake optical drives or traverse network shares.
    return [Path(f"{chr(65 + index)}:/") for index in range(26)
            if mask & (1 << index)
            and kernel32.GetDriveTypeW(f"{chr(65 + index)}:/") in (2, 3)]


def _parse_vdf(text: str) -> dict:
    """Parse Valve KeyValues, including legacy scalar library entries."""
    token_re = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    tokens = []
    cursor = 0
    for match in token_re.finditer(text.lstrip("\ufeff")):
        if match.start() != cursor:
            raise ValueError("Steam libraryfolders.vdf 含无效字符")
        cursor = match.end()
        token = match.group()
        if token.isspace() or token.startswith("//"):
            continue
        if token.startswith('"'):
            token = re.sub(r'\\([\\"])', r'\1', token[1:-1])
            tokens.append((token, False))
        else:
            tokens.append((token, token in ("{", "}")))
    if cursor != len(text.lstrip("\ufeff")):
        raise ValueError("Steam libraryfolders.vdf 字符串未闭合")
    position = 0

    def object_(nested=False):
        nonlocal position
        result = {}
        while position < len(tokens):
            key, structural = tokens[position]
            position += 1
            if structural:
                if key == "}" and nested:
                    return result
                raise ValueError("Steam libraryfolders.vdf 括号不匹配")
            if position >= len(tokens):
                raise ValueError("Steam libraryfolders.vdf 缺少值")
            value, structural = tokens[position]
            position += 1
            if structural:
                if value != "{":
                    raise ValueError("Steam libraryfolders.vdf 缺少值")
                value = object_(True)
            result[key] = value
        if nested:
            raise ValueError("Steam libraryfolders.vdf 括号未闭合")
        return result

    return object_()


def _steam_library_dirs() -> list[Path]:
    roots = list(_registry_steam_dirs())
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if os.environ.get(env_name):
            roots.append(Path(os.environ[env_name]) / "Steam")
    for drive in _drive_roots():
        roots.extend((drive / "SteamLibrary", drive / "Steam"))
    installs = _unique_paths(roots)
    libraries = list(installs)
    for root in installs:
        if not _is_dir(root):
            continue
        for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf = root / relative
            try:
                raw = vdf.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                continue
            parsed = _parse_vdf(raw)
            folders = next((value for key, value in parsed.items()
                            if key.lower() == "libraryfolders"), {})
            if not isinstance(folders, dict):
                raise ValueError(f"Steam 库列表格式错误：{vdf}")
            for number, entry in folders.items():
                if not number.isdecimal():
                    continue
                value = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(value, str) and value:
                    path = Path(value).expanduser()
                    if not path.is_absolute():
                        raise ValueError(f"Steam 库路径必须是绝对路径：{vdf}")
                    libraries.append(path)
    return _unique_paths(libraries)


def _save_candidates(base: Path, local_layout: bool = False) -> list[Path]:
    candidates = []
    steam = base / "Steam"
    if _is_dir(steam):
        for account in sorted(steam.iterdir()):
            # Steam account IDs are decimal; never descend into backup folders.
            if account.name.isdecimal() and _is_dir(account):
                candidates.append(account / SAVE_NAME)
    if local_layout and _is_dir(base):
        candidates.append(base / SAVE_NAME)
        for account in sorted(base.iterdir()):
            if account.name.isdecimal() and _is_dir(account):
                candidates.append(account / SAVE_NAME)
    return [path for path in candidates if _is_file(path)]


def discover_saves() -> list[Path]:
    """Return only live-layout saves, Steam first; no preference writes or scans.

    Missing paths and devices with no media are normal. Permission, malformed
    VDF, and other I/O errors are reported to the caller.
    Manual paths belong in SettingsStore; they are not automatic candidates.
    """
    found = []
    for library in _steam_library_dirs():
        base = library / "steamapps/common/PBZ/ProjectPBZ/Saved/SaveGames"
        found.extend(_save_candidates(base))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    found.extend(_save_candidates(local / "ProjectPBZ/Saved/SaveGames", local_layout=True))
    return _unique_paths(found)


def _user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).expanduser().resolve() / "PBZSaveEditor"
    if os.name == "nt":
        return Path.home() / "AppData/Local/PBZSaveEditor"
    return Path.home() / ".local/share/PBZSaveEditor"


def _json_path(value):
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"设置值不能转为 JSON：{type(value).__name__}")


class SettingsStore:
    """JSON preferences (e.g. save_path and backup_dir), independent of cwd/exe."""

    def __init__(self, base_dir=None):
        self.base_dir = (Path(base_dir).expanduser().resolve()
                         if base_dir is not None else _user_data_dir())
        self.path = self.base_dir / "settings.json"

    def load(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        values = json.loads(text)
        if not isinstance(values, dict):
            raise ValueError(f"用户设置必须是 JSON 对象：{self.path}")
        return values

    def save(self, values: dict) -> None:
        if not isinstance(values, dict):
            raise TypeError("用户设置必须是 dict")
        data = (json.dumps(values, ensure_ascii=False, indent=2,
                           default=_json_path, allow_nan=False) + "\n").encode("utf-8")
        _atomic_write(self.path, data)


def default_backup_dir(base_dir=None) -> Path:
    """Suggest backups without mkdir; pass the portable root to match settings.

    Without an explicit base, use per-user data as SettingsStore() does.
    """
    root = (Path(base_dir).expanduser().resolve() if base_dir is not None
            else _user_data_dir())
    return root / "backups"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes,
                  before_replace: Callable[[], None] | None = None) -> str:
    """Stage/fsync/verify in the target directory, then a single atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        digest = _sha256(data)
        if _sha256(temp_path.read_bytes()) != digest:
            raise OSError(f"待写入文件读回校验失败：{temp_path}")
        if before_replace is not None:
            before_replace()
        os.replace(temp_path, path)
        return digest
    finally:
        # Only this invocation's exclusive temporary file, never a glob cleanup.
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _create_backup(source: Path, original: bytes, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"{source.name}.{timestamp}.{uuid4().hex}.bak"
    # Exclusive creation is mandatory, even when timestamps happen to collide.
    with backup.open("xb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    if _sha256(backup.read_bytes()) != _sha256(original):
        raise OSError(f"备份读回校验失败，已停止保存：{backup}")
    return backup


def create_backup(source: Path, backup_dir: Path, expected_sha256: str | None = None) -> dict:
    """Save a verified disk snapshot without writing to the source file."""
    source = Path(source).expanduser().resolve(strict=True)
    original = source.read_bytes()
    digest = _sha256(original)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise ValueError("源存档已变化，请重新载入后备份")
    backup = _create_backup(source, original, Path(backup_dir).expanduser().resolve())
    return {"backup_path": backup, "source_sha256": digest}


def write_with_backup(source: Path, data: bytes, backup_dir: Path,
                      destination: Path | None = None,
                      expected_sha256: str | None = None) -> dict:
    """Back up verified snapshots before any replacement, or raise.

    Returns Path values for backup_path/output_path and hexadecimal SHA256
    strings for source_sha256/output_sha256. Overwriting a distinct existing
    destination also returns destination_backup_path. A ValueError signals an
    external edit; an appearing destination raises FileExistsError. Other I/O
    errors propagate. Output SHA is verified on the staged bytes before replace.

    The final change check is optimistic: callers should close the game while
    editing. This service does not claim to lock out unrelated writers.
    """
    if not isinstance(data, bytes):
        raise TypeError("存档数据必须是 bytes")
    source = Path(source).expanduser().resolve(strict=True)
    original = source.read_bytes()
    source_sha = _sha256(original)
    if expected_sha256 is not None and source_sha != expected_sha256.lower():
        raise ValueError("源存档已变化（SHA256 不一致），请重新载入后保存")

    output = (Path(destination).expanduser().resolve() if destination is not None
              else source)
    same_path = output == source
    target_original = None
    if not same_path:
        try:
            target_original = output.read_bytes()
        except FileNotFoundError:
            pass
    backup_dir = Path(backup_dir).expanduser().resolve()
    backup = _create_backup(source, original, backup_dir)
    result = {"backup_path": backup, "output_path": output,
              "source_sha256": source_sha}
    if target_original is not None:
        result["destination_backup_path"] = _create_backup(output, target_original, backup_dir)

    def check_unchanged():
        if _sha256(source.read_bytes()) != source_sha:
            raise ValueError("源存档在保存期间发生变化，已停止覆盖")
        if not same_path:
            if target_original is None:
                # lexists also guards against a newly created dangling symlink.
                if os.path.lexists(output):
                    raise FileExistsError(f"另存目标在保存期间出现，已停止覆盖：{output}")
            elif _sha256(output.read_bytes()) != _sha256(target_original):
                raise ValueError("另存目标在保存期间发生变化，已停止覆盖")

    result["output_sha256"] = _atomic_write(output, data, check_unchanged)
    return result


def save_editor(editor, backup_dir: Path, destination: Path | None = None) -> dict:
    """Coordinate the validated save with receipts; pending records match only afterhash."""
    import copy
    from .reward_history import RewardHistory

    data = editor.build()
    after_sha = _sha256(data)
    operations = copy.deepcopy(getattr(editor, 'reward_operations', []))
    for slot, keys in getattr(editor, '_reward_receipts', {}).items():
        if keys:
            operations.append(dict(slot=slot, quest_id='receipt-history',
                                   action='carry_receipts', reward_keys=sorted(keys)))
    history = RewardHistory(backup_dir)
    receipt_path = (history.prepare(editor.source_sha256, after_sha, operations)
                    if operations else None)
    result = write_with_backup(editor.path, data, backup_dir, destination=destination,
                               expected_sha256=editor.source_sha256)
    if receipt_path is not None:
        result['receipt_path'] = receipt_path
        try:
            history.finish(receipt_path)
        except Exception as error:
            result['history_warning'] = ('存档已保存，奖励记录未完成收尾；'
                                         f'匹配文件指纹的待完成记录仍可识别：{error}')
    return result
