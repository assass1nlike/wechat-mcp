"""把微信 4.x 的加密库解成普通 SQLite 文件，放在 data/<微信号>/ 下，供 reader.py 读。

密钥来自 WeChatDataAnalysis 保存的 account_keys.json，只在本进程内存里用，不写进日志和返回值。

库是 SQLCipher 4 格式：每页 4096 字节，末尾 80 字节是 16 字节 IV 加 64 字节 HMAC-SHA512，
正文用 AES-256-CBC 加密；文件开头 16 字节是盐，密钥由它经 PBKDF2-HMAC-SHA512 迭代 256000 次得到。
微信很少把 WAL 合并回主文件，最新的消息通常只在 .db-wal 里，所以解密时把 WAL 里已提交的页一并合并。

每个 Claude Code 会话各起一个 MCP 进程，它们共用 data/ 下的明文副本，所以解密和读取都在同一把文件锁里进行。
"""

import hashlib
import hmac
import json
import os
import re
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import DATA

PAGE, RESERVE, SALT = 4096, 80, 16
BODY = PAGE - RESERVE
ITERATIONS = 256000
SOURCES = ("contact/contact.db", "message/message_[0-9]*.db")
MIN_INTERVAL = 30  # 两次检查之间至少隔这么多秒


class Error(Exception):
    """解密不了的原因，可以直接给用户看。"""


def account(cfg: dict) -> tuple[str, Path, bytes]:
    """返回（微信号，加密库所在的 db_storage 目录，密钥）。"""
    c = cfg.get("wechat", {})
    keys = Path(c.get("keys") or Path(os.environ.get("APPDATA", "")) / "wechat-data-analysis-desktop"
                / "output" / "account_keys.json")
    if not keys.exists():
        raise Error(f"没有找到微信密钥文件 {keys}，需要先用 WeChatDataAnalysis 取一次密钥")
    entries = json.loads(keys.read_text(encoding="utf-8"))
    for entry in sorted(entries.values(), key=lambda e: e.get("updated_at", ""), reverse=True):
        storage = Path(c.get("storage") or entry.get("db_key_source_db_storage_path", ""))
        if entry.get("db_key") and (storage / "contact" / "contact.db").exists():
            wxid = re.sub(r"_[0-9a-f]{4}$", "", storage.parent.name)  # 目录名是微信号加 4 位后缀
            return wxid, storage, bytes.fromhex(entry["db_key"])
    raise Error("密钥文件里记的微信数据目录不存在，在 config.toml 的 [wechat] 里用 storage 指明 db_storage 目录")


def _derive(key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    enc = hashlib.pbkdf2_hmac("sha512", key, salt, ITERATIONS, 32)
    mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(b ^ 0x3A for b in salt), 2, 32)
    return enc, mac


def _valid(mac: bytes, raw: bytes, number: int) -> bool:
    start = SALT if number == 1 else 0
    tag = hmac.new(mac, raw[start:BODY + 16] + struct.pack("<I", number), hashlib.sha512).digest()
    return hmac.compare_digest(tag, raw[BODY + 16:BODY + 80])


def _plain(enc: bytes, raw: bytes, number: int) -> bytes:
    start = SALT if number == 1 else 0
    d = Cipher(algorithms.AES(enc), modes.CBC(raw[BODY:BODY + 16])).decryptor()
    head = b"SQLite format 3\x00" if number == 1 else b""
    return head + d.update(raw[start:BODY]) + d.finalize() + raw[BODY:]


def _wal_pages(wal: bytes, mac: bytes) -> tuple[dict[int, bytes], int]:
    """WAL 里已提交的页（页号 -> 加密页）和提交后的总页数。

    WAL 文件是预分配的，后面留着上一轮的旧帧；旧帧的盐和文件头不一致，遇到就停。
    """
    if len(wal) < 32 or struct.unpack(">I", wal[:4])[0] not in (0x377F0682, 0x377F0683):
        return {}, 0
    salts = wal[16:24]
    done: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    size = 0
    for pos in range(32, len(wal) - PAGE - 23, PAGE + 24):
        number, commit = struct.unpack(">II", wal[pos:pos + 8])
        raw = wal[pos + 24:pos + 24 + PAGE]
        if wal[pos + 8:pos + 16] != salts or number == 0 or not _valid(mac, raw, number):
            break
        pending[number] = raw
        if commit:
            done.update(pending)
            pending.clear()
            size = commit
    return done, size


def _decrypt(src: Path, dst: Path, key: bytes):
    data = src.read_bytes()
    wal_path = src.with_name(src.name + "-wal")
    wal = wal_path.read_bytes() if wal_path.exists() else b""
    if len(data) < PAGE:
        raise Error(f"{src.name} 是空的")
    enc, mac = _derive(key, data[:SALT])
    if not _valid(mac, data[:PAGE], 1):
        raise Error("微信密钥对不上，可能重新登录后换了密钥，需要用 WeChatDataAnalysis 重新取一次")
    pages = {i + 1: data[i * PAGE:(i + 1) * PAGE] for i in range(len(data) // PAGE)}
    newer, size = _wal_pages(wal, mac)
    pages.update(newer)
    total = size or len(pages)
    out = bytearray()
    for number in range(1, total + 1):
        raw = pages.get(number)
        if raw is None or len(raw) < PAGE or not _valid(mac, raw, number):
            raise Error(f"{src.name} 第 {number} 页校验不过，可能微信正在写入，稍后再试")
        out += _plain(enc, raw, number)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    tmp.write_bytes(out)
    os.replace(tmp, dst)


def _signature(src: Path) -> list:
    sig = []
    for p in (src, src.with_name(src.name + "-wal")):
        st = p.stat() if p.exists() else None
        sig += [st.st_mtime, st.st_size] if st else [0, 0]
    return sig


def refresh(cfg: dict) -> tuple[Path, str, str]:
    """让明文副本跟上微信的实时库，只重解有变化的库。调用方需持有 data/.lock。

    返回（明文库目录，自己的微信号，说明）。解密失败但有旧副本时退回旧副本，说明里写清记录只到什么时候。
    """
    wxid, storage, key = account(cfg)
    out = DATA / wxid
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    if time.time() - state.get("checked", 0) < MIN_INTERVAL:
        return out, wxid, ""
    try:
        for pattern in SOURCES:
            for src in sorted(storage.glob(pattern)):
                sig = _signature(src)
                if state.get(src.name) == sig and (out / src.name).exists():
                    continue
                try:
                    _decrypt(src, out / src.name, key)
                except Error:  # 多半是读的时候微信正好在写，隔一会儿重读一次
                    time.sleep(0.5)
                    sig = _signature(src)
                    _decrypt(src, out / src.name, key)
                state[src.name] = sig
    except (Error, OSError) as e:
        if not (out / "message_0.db").exists():
            raise Error(f"读不了微信：{e}") from e
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime((out / "message_0.db").stat().st_mtime))
        return out, wxid, f"（最新的微信记录解密失败：{e}。下面是旧副本，只到 {t} 左右）\n"
    state["checked"] = time.time()
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return out, wxid, ""
