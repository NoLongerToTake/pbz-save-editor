"""PBZS authentication, Oodle decode, and sparse JSON editing primitives; no quest logic."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "deps"))

KEY = bytes.fromhex("15a1fe0f55e0eb298e7c9461548351696a2a0700ed2d17ba1f871457e8f93e2d")
ENVELOPE = b"PBZS\x01\x01\x00\x00"

def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


DECODER = json.JSONDecoder(parse_float=Decimal, object_pairs_hook=no_duplicate_keys)


def skip_space(text, pos):
    while pos < len(text) and text[pos] in " \r\n\t":
        pos += 1
    return pos


def members(text, start):
    """Find immediate object values without changing their original text."""
    if text[start] != "{":
        raise ValueError("Expected a JSON object")
    result = {}
    pos = skip_space(text, start + 1)
    while text[pos] != "}":
        key_start = pos
        key, pos = DECODER.raw_decode(text, pos)
        pos = skip_space(text, pos)
        if text[pos] != ":":
            raise ValueError("Missing JSON colon")
        value_start = skip_space(text, pos + 1)
        _, value_end = DECODER.raw_decode(text, value_start)
        result[key] = (key_start, value_start, value_end)
        pos = skip_space(text, value_end)
        if text[pos] == "}":
            break
        if text[pos] != ",":
            raise ValueError("Missing JSON comma")
        pos = skip_space(text, pos + 1)
    return result


def elements(text, start):
    if text[start] != "[":
        raise ValueError("Expected a JSON array")
    pos = skip_space(text, start + 1)
    while text[pos] != "]":
        value, end = DECODER.raw_decode(text, pos)
        yield pos, end, value
        pos = skip_space(text, end)
        if text[pos] == "]":
            break
        if text[pos] != ",":
            raise ValueError("Missing JSON array comma")
        pos = skip_space(text, pos + 1)


def apply_edits(text, edits):
    previous = len(text) + 1
    for start, end, replacement in sorted(edits, reverse=True):
        if not 0 <= start <= end <= len(text) or end > previous:
            raise ValueError("Overlapping or invalid text edits")
        text = text[:start] + replacement + text[end:]
        previous = start
    return text


def pack_uncompressed(raw):
    return b"".join((b"\xcc\x0a" if pos == 0 else b"\x4c\x0a") + raw[pos:pos + 0x40000]
                    for pos in range(0, len(raw), 0x40000))


def open_block(block):
    from Crypto.Cipher import AES
    if len(block) < 36 or block[:8] != ENVELOPE:
        raise ValueError("Invalid encrypted block header")
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=block[8:20])
    return cipher.decrypt_and_verify(block[36:], block[20:36])


def seal_block(packed):
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes
    iv = get_random_bytes(12)
    ciphertext, tag = AES.new(KEY, AES.MODE_GCM, nonce=iv).encrypt_and_digest(packed)
    return ENVELOPE + iv + tag + ciphertext


def read_archive(data):
    if len(data) < 12 or data[:6] != b"PBZS\x03\x01":
        raise ValueError("Unsupported PBZ archive header")
    count, toc_size = struct.unpack_from("<HI", data, 6)
    base = 12 + toc_size
    toc = open_block(data[12:base])
    if toc[:12] != data[:12]:
        raise ValueError("File and TOC headers disagree")
    pos = 12
    entries = []
    expected_offset = 0
    for _ in range(count):
        name_size = struct.unpack_from("<H", toc, pos)[0]
        pos += 2
        name = toc[pos:pos + name_size].decode("utf-8")
        pos += name_size
        flags = toc[pos:pos + 2]
        if flags != b"\x02\x01":
            raise ValueError(f"Unexpected flags for {name}")
        pos += 2
        toc_pos = pos
        raw_size, offset, block_size = struct.unpack_from("<III", toc, pos)
        pos += 12
        if offset != expected_offset or base + offset + block_size > len(data):
            raise ValueError(f"Invalid block offset/size for {name}")
        block = data[base + offset:base + offset + block_size]
        entries.append(dict(name=name, raw_size=raw_size, offset=offset,
                            block_size=block_size, toc_pos=toc_pos, block=block,
                            packed=open_block(block)))
        expected_offset += block_size
    if pos != len(toc) or base + expected_offset != len(data):
        raise ValueError("Archive/TOC total byte count mismatch")
    if len({entry["name"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate archive entry names")
    return entries, toc


def decode_payload(entry, decoder):
    result = subprocess.run([str(decoder), str(entry["raw_size"])],
                            input=entry["packed"], capture_output=True, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode != 0:
        raise ValueError(f"Oodle decode failed for {entry['name']}: "
                         + result.stderr.decode("utf-8", errors="replace")[:1000])
    if len(result.stdout) != entry["raw_size"]:
        raise ValueError(f"Oodle output length mismatch for {entry['name']}")
    DECODER.decode(result.stdout.decode("utf-8"))
    return result.stdout


def json_differences(old, new, path=""):
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in sorted(old.keys() | new.keys()):
            location = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in old or key not in new:
                changes.append(dict(path=location, before=old.get(key), after=new.get(key)))
            else:
                changes.extend(json_differences(old[key], new[key], location))
        return changes
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        changes = []
        for index, (before, after) in enumerate(zip(old, new)):
            changes.extend(json_differences(before, after, path + "/" + str(index)))
        return changes
    return [dict(path=path, before=old, after=new)]


