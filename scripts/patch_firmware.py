#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenWrt / iStoreOS 刷机镜像定制器
=================================

在 Windows / Linux / macOS 上直接改写 .img(.gz) 刷机镜像里的内置配置，
最典型的用途是把出厂默认 LAN IP 改成你想要的地址 ——
做到「刷完开机直接就是目标 IP」，省掉刷完再进后台改地址那一步。

它会自动完成：
  1. 解析镜像（gzip + OpenWrt fwtool 元数据段）
  2. 解析 MBR/GPT 分区表，定位 squashfs 根文件系统
  3. 用 sqfs2tar 导出（完整保留 Unix 权限/属主/链接/设备号）
  4. 定位并替换目标字符串（默认：iStoreOS 的 lan 默认 IP）
  5. 用 tar2sqfs 按原镜像参数 1:1 重建
  6. 按偏移回写分区，保持分区表 / U-Boot / 数据区标记不变
  7. **重新计算并追加 fwtool 元数据段**（漏掉它网页升级会报
     "Image metadata not present"）
  8. 自校验：逐文件哈希比对 + 元数据 CRC 验证

依赖：squashfs-tools-ng（提供 sqfs2tar / tar2sqfs / rdsquashfs）。
Windows 用户可直接下载原生 MinGW 预编译包（解压即用）：
    https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip

示例：
    # 把 iStoreOS 镜像的默认 LAN IP 改成 192.168.1.1
    python patch_firmware.py -i istoreos-25.12.5-r2s-squashfs.img.gz \
                             -o istoreos-25.12.5-r2s-192.168.1.1.img.gz \
                             --lan-ip 192.168.1.1 \
                             --tools-dir tools/squashfs-tools-ng-1.3.2-mingw64/bin

    # 通用字符串替换（任意长度都会正确处理）
    python patch_firmware.py -i in.img.gz -o out.img.gz \
                             --replace 'old text' 'new text' --in-file bin/config_generate
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

FW_MAGIC = 0x46577830                 # "FWx0"，OpenWrt fwtool 元数据尾标
FW_TRAILER_SIZE = 16
SQFS_MAGIC = b"hsqs"                   # squashfs 小端
COMPRESSORS = {1: "gzip", 2: "lzma", 3: "lzo", 4: "xz", 5: "lz4", 6: "zstd"}
SB_FLAG_UNCOMPRESSED_XATTRS = 0x0100
SB_FLAG_NO_XATTRS = 0x0200
SB_FLAG_EXPORTABLE = 0x0080

# iStoreOS / OpenWrt 生成 lan 默认地址的位置
LAN_IP_RE = re.compile(rb'ipad=\$\{ipaddr:-"(\d{1,3}(?:\.\d{1,3}){3})"\}')


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1):
    print("\n[错误] " + msg, file=sys.stderr, flush=True)
    sys.exit(code)


# --------------------------------------------------------------------------
# fwtool 元数据段
# --------------------------------------------------------------------------

def split_metadata(raw: bytes):
    """拆出 (正文, fwtool元数据段)。找不到元数据段时第二项为 b""。"""
    idx = raw.rfind(b"FWx0")
    while idx >= 0:
        if idx + FW_TRAILER_SIZE <= len(raw):
            magic, crc, typ, _pad, size = struct.unpack(
                ">II B 3s I", raw[idx:idx + FW_TRAILER_SIZE])
            start = idx + FW_TRAILER_SIZE - size
            if magic == FW_MAGIC and 0 <= start <= idx:
                return raw[:start], raw[start:]
        idx = raw.rfind(b"FWx0", 0, idx)
    return raw, b""


def parse_metadata_tail(tail: bytes):
    """解析元数据段 -> dict(header, json, trailer)"""
    if not tail:
        return None
    magic, crc, typ, _pad, size = struct.unpack(">II B 3s I", tail[-FW_TRAILER_SIZE:])
    header = tail[0:8]
    js = tail[8:len(tail) - FW_TRAILER_SIZE]
    return {
        "magic": magic, "crc": crc, "type": typ, "size": size,
        "header": header, "json_raw": js,
        "json": json.loads(js.decode("utf-8")),
    }


def build_metadata_tail(gzstream: bytes, template: bytes) -> bytes:
    """用模板里的 header+JSON 生成新元数据段，CRC 覆盖 gzstream+header+json。"""
    if not template:
        return b""
    header, js = template[0:8], template[8:-FW_TRAILER_SIZE]
    body = gzstream + header + js
    crc = zlib.crc32(body) ^ 0xFFFFFFFF          # init=0xFFFFFFFF, 无最终取反
    trailer = struct.pack(">II B 3s I", FW_MAGIC, crc, 1, b"\x00\x00\x00",
                          FW_TRAILER_SIZE + 8 + len(js))
    return header + js + trailer


def verify_metadata(raw: bytes) -> bool:
    tail = split_metadata(raw)[1]
    if not tail:
        return True                                   # 本来就没有 = 无要求
    magic, crc, _typ, _pad, _size = struct.unpack(">II B 3s I", tail[-16:])
    return (zlib.crc32(raw[:-16]) ^ 0xFFFFFFFF) == crc


# --------------------------------------------------------------------------
# gzip
# --------------------------------------------------------------------------

def gunzip(raw: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return d.decompress(raw)


def gzip_bytes(data: bytes, level: int = 9) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf,
                       compresslevel=level, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


# --------------------------------------------------------------------------
# 分区表
# --------------------------------------------------------------------------

def parse_partitions(data: bytes):
    """返回 [(label, offset, size), ...]，支持 MBR，GPT 头存在时改用 GPT。"""
    parts = []
    if len(data) < 1024:
        die("镜像太小，不是有效的磁盘镜像")

    gpt = data[512:520] == b"EFI PART"
    if gpt:
        (_sig, _rev, _hsz, _crc, _r1, _cur, _bak, _first, _last, _guid,
         pstart, pcount, psz, _pcrc) = struct.unpack("<8sIIIIQQQQ16sQIII", data[512:512 + 92])
        for i in range(pcount):
            off = pstart * 512 + i * psz
            ent = data[off:off + psz]
            if len(ent) < 48 or ent[:16] == b"\x00" * 16:
                continue
            s, e = struct.unpack("<QQ", ent[32:48])
            if s == 0:
                continue
            name = ent[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
            parts.append((name or "p%d" % (i + 1), s * 512, (e - s + 1) * 512))
        return parts

    if data[510:512] != b"\x55\xaa":
        die("既不是 GPT 也没有合法的 MBR 签名，无法解析分区表")
    for i in range(4):
        e = data[446 + 16 * i:446 + 16 * (i + 1)]
        ptype = e[4]
        if ptype == 0:
            continue
        lba, cnt = struct.unpack("<II", e[8:16])
        if cnt == 0:
            continue
        parts.append(("p%d(0x%02x)" % (i + 1, ptype), lba * 512, cnt * 512))
    return parts


def find_squashfs(data: bytes, parts):
    """在分区表里（找不到就全盘扫描）定位 squashfs 分区。"""
    for label, off, size in parts:
        if off + 4 <= len(data) and data[off:off + 4] == SQFS_MAGIC:
            return label, off, size
    # 退化：全盘找 superblock（magic + 合理的 block_size）
    for m in re.finditer(re.escape(SQFS_MAGIC), data[:64 << 20]):
        off = m.start()
        if off + 96 <= len(data):
            bs = struct.unpack("<I", data[off + 12:off + 16])[0]
            if bs in (4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576):
                return "scan", off, len(data) - off
    die("没找到 squashfs 分区（magic 'hsqs'）")


def read_superblock(data: bytes, off: int) -> dict:
    sb = data[off:off + 96]
    if sb[:4] != SQFS_MAGIC:
        die("指定偏移处不是 squashfs")
    (magic, inodes, mtime, block_size, frags, comp, blog,
     flags, no_ids, major, minor) = struct.unpack("<IIIIIHHHHHH", sb[:32])
    bytes_used = struct.unpack("<Q", sb[40:48])[0]
    if comp not in COMPRESSORS:
        die("未知的 squashfs 压缩算法编号 %d" % comp)
    return {
        "inodes": inodes, "block_size": block_size, "fragments": frags,
        "compression": comp, "compressor": COMPRESSORS[comp],
        "flags": flags, "version": "%d.%d" % (major, minor),
        "bytes_used": bytes_used or (len(data) - off),
    }


# --------------------------------------------------------------------------
# squashfs-tools-ng 封装
# --------------------------------------------------------------------------

def find_tool(tools_dir, name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    cand = os.path.join(tools_dir, exe)
    if os.path.isfile(cand):
        return cand
    found = shutil.which(name)
    if found:
        return found
    die("找不到 %s，请用 --tools-dir 指定 squashfs-tools-ng 的 bin 目录\n"
        "     下载：https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip"
        % exe)


def run(cmd, stdin=None, stdout=None):
    """统一调用外部工具。

    注意：必须用参数列表调用。Git Bash 等 MSYS 环境会做 POSIX→Windows 路径转换，
    把 `/etc/config/network` 这类参数改写成 `C:/Program Files/Git/etc/...`，
    导致工具报 "no such file or directory"。绕开 shell 是唯一可靠做法。
    """
    p = subprocess.run(
        cmd,
        stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        die("命令失败 (%d): %s\n%s" % (
            p.returncode, " ".join(map(str, cmd)),
            (p.stderr or b"").decode("utf-8", "replace")[:1200]))
    return p


def sqfs_to_tar(tools_dir, sqfs_path, tar_path):
    log("    sqfs2tar ...")
    with open(tar_path, "wb") as fo:
        run([find_tool(tools_dir, "sqfs2tar"), sqfs_path], stdout=fo)


def tar_to_sqfs(tools_dir, tar_path, sqfs_path, sb, jobs=None):
    cmd = [find_tool(tools_dir, "tar2sqfs"),
           "-c", sb["compressor"], "-b", str(sb["block_size"])]
    if sb["flags"] & SB_FLAG_NO_XATTRS:
        cmd.append("-x")
    if sb["flags"] & SB_FLAG_EXPORTABLE:
        cmd.append("-e")
    cmd += ["-j", str(jobs or max(2, (os.cpu_count() or 4))), "-f", "-q", "-s", sqfs_path]
    log("    tar2sqfs (%s, block=%d) ..." % (sb["compressor"], sb["block_size"]))
    with open(tar_path, "rb") as fin:
        run(cmd, stdin=fin)


# --------------------------------------------------------------------------
# tar 内容替换
# --------------------------------------------------------------------------

def collect_files(tar_path):
    with tarfile.open(tar_path, "r:") as t:
        return [m.name for m in t.getmembers() if m.isfile() and not m.islnk()]


def patch_tar(tar_path, out_path, replacer):
    """
    遍历 tar，对每个普通文件调用 replacer(name, data) -> new_data 或 None。
    用 tarfile 重建，因此新旧内容长度不同也完全正确。
    返回 [(name, count), ...]，count 为改动次数。
    """
    changed = []
    with tarfile.open(tar_path, "r:") as tin, \
            tarfile.open(out_path, "w:", format=tarfile.PAX_FORMAT) as tout:
        for m in tin.getmembers():
            fobj = tin.extractfile(m) if m.isfile() and not m.islnk() else None
            data = fobj.read() if fobj is not None else None
            new = replacer(m.name, data) if data is not None else None
            if new is not None and new != data:
                changed.append((m.name, 1))
                m.size = len(new)
                tout.addfile(m, io.BytesIO(new))
            elif data is not None:
                tout.addfile(m, io.BytesIO(data))
            else:
                tout.addfile(m)
    return changed


def make_lan_ip_replacer(new_ip: str):
    new_ip_b = new_ip.encode()

    def replacer(name, data):
        if data is None:
            return None
        if LAN_IP_RE.search(data):
            out = LAN_IP_RE.sub(lambda m: b'ipad=${ipaddr:-"%s"}' % new_ip_b, data)
            return out
        return None

    return replacer


def make_string_replacer(old: str, new: str, only_file=None):
    old_b, new_b = old.encode(), new.encode()

    def replacer(name, data):
        if data is None:
            return None
        if only_file and name.lstrip("./") != only_file.lstrip("./"):
            return None
        if old_b not in data:
            return None
        return data.replace(old_b, new_b)

    return replacer


# --------------------------------------------------------------------------
# 完整性校验
# --------------------------------------------------------------------------

def index_tar(tar_path):
    """{name: (type, mode, uid, gid, linkname, size, sha256|None)}"""
    idx = {}
    with tarfile.open(tar_path, "r:") as t:
        for m in t.getmembers():
            h = None
            if m.isfile() and not m.islnk():
                f = t.extractfile(m)
                d = hashlib.sha256()
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    d.update(b)
                h = d.hexdigest()
            idx[m.name] = (m.type, m.mode, m.uid, m.gid, m.linkname, m.size, h)
    return idx


def sha256_file(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="改写 OpenWrt / iStoreOS 刷机镜像里的内置配置（默认定制 LAN IP）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", required=True, help="原始 .img 或 .img.gz（不会被修改）")
    ap.add_argument("-o", "--output", required=True, help="输出镜像路径（.img.gz 会顺带写 .img）")
    ap.add_argument("--lan-ip", help="把 lan 默认 IP 改成这个地址（iStoreOS/OpenWrt 场景）")
    ap.add_argument("--replace", nargs=2, metavar=("OLD", "NEW"),
                    help="通用字符串替换（配合 --in-file 限定文件）")
    ap.add_argument("--in-file", help="只在该路径内替换，例如 bin/config_generate")
    ap.add_argument("--tools-dir", default=".", help="squashfs-tools-ng 的 bin 目录")
    ap.add_argument("--jobs", type=int, help="tar2sqfs 并行任务数")
    ap.add_argument("--workdir", help="中间文件目录（默认用系统临时目录）")
    ap.add_argument("--no-verify", action="store_true", help="跳过逐文件比对（不推荐）")
    args = ap.parse_args(argv)

    if not args.lan_ip and not args.replace:
        die("至少要指定 --lan-ip 或 --replace OLD NEW")

    work = args.workdir or tempfile.mkdtemp(prefix="fwpatch_")
    os.makedirs(work, exist_ok=True)

    # ---------- 1. 读入 ----------
    log("[1/8] 读取镜像 %s" % os.path.basename(args.input))
    raw = open(args.input, "rb").read()
    log("      文件大小 %d 字节" % len(raw))

    is_gz = raw[:2] == b"\x1f\x8b"
    body, meta_tail = (split_metadata(raw) if is_gz else (raw, b""))
    meta = parse_metadata_tail(meta_tail)
    if meta:
        log("      发现 fwtool 元数据段：board=%s type=%d size=%d"
            % (meta["json"].get("version", {}).get("board", "?"), meta["type"], meta["size"]))
    else:
        log("      （无 fwtool 元数据段 —— 原始文件没有，输出也不会补）")

    img = gunzip(body) if is_gz else body
    log("      解压后 %d 字节" % len(img))

    # ---------- 2. 分区 ----------
    log("[2/8] 解析分区表")
    parts = parse_partitions(img)
    for label, off, size in parts:
        log("      %-12s offset=%-12d size=%d" % (label, off, size))
    label, sq_off, sq_size = find_squashfs(img, parts)
    log("      squashfs 分区：%s @ %d" % (label, sq_off))

    sb = read_superblock(img, sq_off)
    log("      superblock: inodes=%d block=%d comp=%s flags=0x%04x used=%d"
        % (sb["inodes"], sb["block_size"], sb["compressor"], sb["flags"], sb["bytes_used"]))
    old_sq_size = sb["bytes_used"]

    # ---------- 3. 导出 ----------
    log("[3/8] 导出 rootfs")
    orig_sqfs = os.path.join(work, "orig.squashfs")
    with open(orig_sqfs, "wb") as f:
        f.write(img[sq_off:sq_off + old_sq_size])
    tar1 = os.path.join(work, "orig.tar")
    sqfs_to_tar(args.tools_dir, orig_sqfs, tar1)
    log("      tar 大小 %d 字节" % os.path.getsize(tar1))

    # ---------- 4. 替换 ----------
    log("[4/8] 执行替换")
    replacer = (make_lan_ip_replacer(args.lan_ip) if args.lan_ip
                else make_string_replacer(args.replace[0], args.replace[1], args.in_file))
    tar2 = os.path.join(work, "patched.tar")
    changed = patch_tar(tar1, tar2, replacer)
    if not changed:
        die("没有找到任何匹配的目标内容，请检查参数")
    for name, _ in changed:
        log("      已修改：%s" % name)
    if args.lan_ip:
        log("      lan 默认 IP -> %s" % args.lan_ip)

    # ---------- 5. 重建 squashfs ----------
    log("[5/8] 重建 squashfs")
    new_sqfs = os.path.join(work, "new.squashfs")
    tar_to_sqfs(args.tools_dir, tar2, new_sqfs, sb, args.jobs)
    new_size = os.path.getsize(new_sqfs)
    log("      %d 字节（原 %d，差 %+d）" % (new_size, old_sq_size, new_size - old_sq_size))
    if sq_off + new_size > sq_off + sq_size:
        die("新 squashfs 超出分区容量（%d > %d）" % (new_size, sq_size))

    # ---------- 6. 回写 ----------
    log("[6/8] 回写镜像")
    out_img = bytearray(img)
    out_img[sq_off:sq_off + new_size] = open(new_sqfs, "rb").read()
    img_bytes = bytes(out_img)

    # ---------- 7. 元数据 ----------
    log("[7/8] 处理 fwtool 元数据段")
    if is_gz:
        gzstream = gzip_bytes(img_bytes)
        tail = build_metadata_tail(gzstream, meta_tail)
        final = gzstream + tail
        log("      gzip 流 %d 字节 + 元数据 %d 字节" % (len(gzstream), len(tail)))
    else:
        final = img_bytes
        log("      输入是未压缩 .img，输出同样不压缩")

    out_path = args.output
    with open(out_path, "wb") as f:
        f.write(final)
    log("      已写出 %s (%d 字节)" % (out_path, len(final)))

    # 同时落一份解压版，方便 Rufus / RKDevTool 线刷
    base = os.path.splitext(out_path)[0]
    img_out = base if base.lower().endswith(".img") else base + ".img"
    if is_gz:
        with open(img_out, "wb") as f:
            f.write(img_bytes)
        log("      已写出 %s (%d 字节) —— 仅供线刷/烧卡，不能用于网页升级" % (img_out, len(img_bytes)))

    # ---------- 8. 校验 ----------
    log("[8/8] 校验")
    ok = True

    chk = read_superblock(img_bytes, sq_off)
    same_sb = (chk["inodes"] == sb["inodes"] and chk["block_size"] == sb["block_size"]
               and chk["compression"] == sb["compression"])
    log("      superblock 一致性：%s" % ("OK" if same_sb else "失败"))
    ok &= same_sb

    if not args.no_verify:
        tar3 = os.path.join(work, "reread.tar")
        sqfs_to_tar(args.tools_dir, new_sqfs, tar3)
        a, b = index_tar(tar2), index_tar(tar3)
        only_a = set(a) - set(b)
        only_b = set(b) - set(a)
        diffs = [n for n in set(a) & set(b) if a[n] != b[n]]
        log("      回读比对：条目 %d / 仅A %d / 仅B %d / 差异 %d"
            % (len(a), len(only_a), len(only_b), len(diffs)))
        if only_a or only_b or diffs:
            log("      !! 差异条目：%s" % sorted(only_a | only_b | set(diffs))[:10])
            ok = False

    if is_gz:
        if meta_tail:
            v = verify_metadata(final)
            log("      元数据 CRC 自校验：%s" % ("OK" if v else "失败"))
            ok &= v
        back = gunzip(split_metadata(final)[0])
        same_payload = hashlib.sha256(back).hexdigest() == hashlib.sha256(img_bytes).hexdigest()
        log("      解压回读一致性：%s" % ("OK" if same_payload else "失败"))
        ok &= same_payload

    log("\n      sha256 = %s" % sha256_file(out_path))
    log("\n%s" % ("全部校验通过 ✅" if ok else "存在校验失败 ❌"))

    if not args.workdir:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
