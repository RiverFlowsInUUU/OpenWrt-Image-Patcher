---
name: openwrt-image-default-ip-patch
description: 在 Windows / Linux / macOS 上直接改写 OpenWrt / iStoreOS 刷机镜像（.img / .img.gz）的内置默认配置——最典型是改出厂默认 LAN IP（如 192.168.100.1 → 192.168.1.1），也适用于改 hostname、默认密码、预置 uci 配置。触发词：改刷机镜像默认IP、固件默认后台地址、iStoreOS 镜像改IP、OpenWrt img 改配置、改 squashfs 固件、刷完直接是某个IP、R2S 固件定制、squashfs 重打包、Image metadata not present、Image check failed、sysupgrade 不认镜像、网页升级报错。核心链路：sqfs2tar 保权限 → 改 tar → tar2sqfs 1:1 重建 → 按偏移回写分区 → 重算并追加 fwtool 元数据段。
agent_created: true
---

# 改写 OpenWrt / iStoreOS 刷机镜像的内置配置

把一个官方 `.img(.gz)` 变成「刷完开机就是我要的默认配置」的版本，
**不动原始文件**，且不破坏 U-Boot / 分区表 / 文件权限。

---

## 0. 首选：直接用配套脚本

仓库里的 `scripts/patch_firmware.py` 已把全流程（含元数据段处理与自校验）封好：

```bash
python scripts/patch_firmware.py \
    -i istoreos-25.12.5-r2s-squashfs.img.gz \
    -o istoreos-25.12.5-r2s-lan192.168.1.1.img.gz \
    --lan-ip 192.168.1.1 \
    --tools-dir tools/squashfs-tools-ng-1.3.2-mingw64/bin
```

**参数化、动态解析**（分区表与 squashfs 位置都不写死），结尾打印逐文件比对结果，
任一项失败以非零码退出。**只有脚本报错、或场景超出其能力（如改的是 ext4 分区里的
内容、需要动 U-Boot）时才走下面的手动流程。**

---

## 1. 依赖：squashfs-tools-ng

需要 `sqfs2tar` / `tar2sqfs` / `rdsquashfs`。

- **Windows**：原生 MinGW 预编译包，解压即用（自带全部 DLL）——
  `https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip`
  （目录列表 `https://infraroot.at/pub/squashfs/windows/` 里挑最新版）
- **Linux**：`apt install squashfs-tools-ng` / `dnf install squashfs-tools-ng`
- **macOS**：`brew install squashfs-tools-ng`

不要指望 WSL —— 有些环境里它被安全策略禁用（调用直接报
`PROGRAM BLOCKED BY SECURITY POLICY`）且无法绕过。同理别默认 `docker` / `gcc` /
`7z` / `unsquashfs` 一定存在。**先用上面三个原生二进制解决问题。**

---

## 2. ⚠️ 四个必踩的坑（先看这里，省一小时）

1. **MSYS 路径转换会把参数搞烂。**
   Git Bash 调用这些 exe 时，参数 `/etc/config/network` 会被自动改写成
   `C:/Program Files/Git/etc/config/network` → 报
   `reading filesystem tree: no such file or directory`。
   实测 `MSYS_NO_PATHCONV=1` / `MSYS2_ARG_CONV_EXCL='*'` 都不一定管用。
   **解法：一律用 Python `subprocess.run([...])` 直接调用，不经过 shell。**

2. **`rdsquashfs -l` 也需要一个路径参数。**
   正确写法 `rdsquashfs -l <path> <image>`。只给 image 会报 `Missing image argument`，
   极易误判成工具坏了。同理 `-c <path> <image>`、`-s <path> <image>`。

3. **绝对不要用 `rdsquashfs -u` 解包到磁盘再 `gensquashfs` 打包。**
   Windows/NTFS 存不了 Unix mode/uid/gid，重建出来**所有文件权限变默认值** →
   `/sbin/init`、`/etc/init.d/*` 不可执行，系统直接起不来。**必须走 tar 中转**（见 §4）。

4. **重新 gzip 后必须把 fwtool 元数据段补回去。**
   否则网页升级直接报 `Image metadata not present` + `Image check failed.`
   —— 最容易漏、后果最严重的一步（见 §6）。

---

## 3. 定位真正的生效点（别猜，全量搜）

```python
# 1) 解析 MBR 得各分区 offset/size（GPT 头在 LBA1，若 data[512:520]==b'EFI PART' 另解）
# 2) 探测分区起始魔数：b'hsqs' = squashfs(LE)；ext4 看 offset+1024+56 == b'\x53\xef'
# 3) 读 squashfs superblock（96B）拿 inodes/block_size/compression/flags/bytes_used
```

superblock 字段（小端）：

```
u32 magic('hsqs'), u32 inodes, u32 mkfs_time, u32 block_size, u32 fragments,
u16 compression(1=gzip 2=lzma 3=lzo 4=xz 5=lz4 6=zstd), u16 block_log,
u16 flags, u16 no_ids, u16 major, u16 minor,
u64 root_inode, u64 bytes_used, u64 id_table, u64 xattr_id_table,
u64 inode_table, u64 dir_table, u64 frag_table, u64 lookup_table
```

然后 **把整个 rootfs 导出成 tar，遍历 tar 全量搜目标字符串** —— 不要只 grep 显而易见的文件：

```python
with open('rootfs.tar', 'rb') as f:
    data = f.read()          # 几十~几百 MB，直接全读
```

### iStoreOS 实测结论（25.x）

出厂默认 LAN IP 的**唯一生效点**是 `/bin/config_generate` 里的：

```sh
lan) ipad=${ipaddr:-"192.168.100.1"} ;;
```

其余命中（`usr/lib/lua/luci/controller/admin/ota.lua`、
`www/luci-static/quickstart/index.js`、`www/luci-static/resources/view/system/flash.js`、
Go 二进制 `usr/sbin/quickstart`）**全是 UI 提示文案 / 示例字符串**，
改了没用还可能把二进制改坏。**只改 `config_generate`。**

（`/etc/config/network` 在镜像里**根本不存在**，是首次启动时由 `config_generate` 生成的。）

---

## 4. 修改 + 重建

### 4.1 改

两种策略，按长度是否相同选：

- **等长**（例如 `192.168.100.1` ⇄ `192.168.1.100`，都是 13 字符）：
  直接 `data.replace(old, new)` 改 tar 字节，**后续成员偏移全不变**，最安全。
- **不等长**：用 `tarfile` 遍历重建（`getmembers()` → 改 `m.size` → `addfile()`），
  它会自动处理对齐。重建后**必须**跑 §7 的逐文件比对来证明无损。

长度不等又不想重建 tar 时的凑数技巧：在 `case` 分支 `lan)` 后加/减空格
（shell 里无语义），但**别在 `ipad=` 与 `${...}` 之间插空格** —— 那会把赋值变成空串 + 执行命令。

### 4.2 重建 squashfs：参数必须 1:1 复刻原镜像

| 原 superblock | tar2sqfs 参数 |
|---|---|
| `compression=4` (xz) | `-c xz` |
| `block_size=262144` | `-b 262144` |
| `flags & 0x0200` (NO_XATTRS) | `-x` |
| `flags & 0x0080` (EXPORTABLE) | `-e` |

```python
cmd = [tar2sqfs, "-c", compressor, "-b", str(block_size)]
if flags & 0x0200: cmd.append("-x")
if flags & 0x0080: cmd.append("-e")
cmd += ["-j", str(os.cpu_count()), "-f", "-q", "-s", out_sqfs]
subprocess.run(cmd, stdin=open(patched_tar, "rb"))     # tar2sqfs 从 stdin 读
```

`-s` = `--no-skip`：遇到读不了的记录直接失败而不是静默跳过，是道安全网。

重建后大小会差几十~几百 KB，**正常**（压缩块划分不同）。只要
`新大小 ≤ 分区声明大小` 就放得下。实测：151.92 MB → 152.22 MB。

---

## 5. 回写镜像

```python
img = bytearray(open(orig_img, 'rb').read())
img[sq_off : sq_off + len(new_sqfs)] = open(new_sqfs, 'rb').read()
# 断言 sq_off + len(new_sqfs) <= sq_off + part_size
open('new.img', 'wb').write(img)     # 总大小与原镜像保持一致
```

保持总长度不变，分区表 / U-Boot / 数据区标记就都不用动。

### NanoPi R2S / iStoreOS 实测布局（可作他机参考）

| 区域 | 偏移 | 大小 | 内容 |
|---|---|---|---|
| — | 0 | 16 MB | Rockchip U-Boot（idbloader + u-boot.itb）|
| p1 | 16,777,216 | 64 MB | ext4（boot / kernel）|
| p2 | 84,934,656 | 256 MB | **squashfs rootfs** ← 改这里 |
| p3 | 354,418,688 | 声明 2048 MB，镜像内仅 512 B | `RESET000\n` 标记（rootfs_data）|

---

## 6. ⚠️ 必须把 fwtool 元数据段补回去

iStoreOS / OpenWrt 的 `.img.gz` **不只是 gzip**，gzip 流之后还追加了一段 fwtool 元数据。
**自己重新 gzip 会把它丢掉** → 网页升级报
`Image metadata not present` / `Image check failed.`
（sysupgrade 从文件**末尾**倒着找 `FWx0` 标记做设备与版本校验，找不到就拒绝。）

**结构**（用 `zlib.decompressobj(16 + gzip.MAX_WBITS)` 解压，`unused_data` 就是这段）：

```
[ gzip 流 ]                  ← 压缩后的磁盘镜像
[ 8 字节 fwimage_header ]    ← 实测全 0x00
[ JSON 元数据文本 + "\n" ]
[ 16 字节 fwimage_trailer ]
```

`fwimage_trailer`（C 结构体带对齐，实际 16 字节）：

```c
#define FWIMAGE_MAGIC 0x46577830   /* "FWx0" */
struct fwimage_trailer {
    uint32_t magic;   /* BE, "FWx0" */
    uint32_t crc32;   /* BE */
    uint8_t  type;    /* 1 = FWIMAGE_INFO（元数据）; 0 = 签名 */
    /* uint8_t __pad + 2 字节对齐填充 = 共 3 字节 0x00 */
    uint32_t size;    /* BE，= header(8) + JSON + 16 */
};
```

**crc32 算法**（用 25.12.5-2026091113 与 24.10.8-2026073111 两个官方样本交叉验证通过）：

```python
crc32 = zlib.crc32(整个文件去掉最后 16 字节) ^ 0xFFFFFFFF
# 等价于 crc32_block(init=0xFFFFFFFF, data)，多项式 0xEDB88320，无最终取反
# 覆盖范围包含 gzip 流本身 → 只要重新 gzip 过就必须重算
```

⚠️ **不要用逐字节 Python 循环** —— 169 MB 会跑到超时。用上面这个 zlib 恒等式
（推导：`zlib.crc32(data, 0xFFFFFFFF) == crc_raw(0, data) ^ 0xFFFFFFFF`）。

**补回去**：原始 `[header+JSON]` 在版本/board 未变时字节完全相同，直接复用，只重算 crc：

```python
orig = open(orig_gz, 'rb').read()
d = zlib.decompressobj(16 + zlib.MAX_WBITS); d.decompress(orig)
rest = orig[len(orig) - len(d.unused_data):]     # [header + JSON + trailer]
HDR, JS = rest[0:8], rest[8:-16]

body = gzstream + HDR + JS
crc  = zlib.crc32(body) ^ 0xFFFFFFFF
trailer = struct.pack(">II B 3s I", 0x46577830, crc, 1, b"\x00\x00\x00", len(rest))

open(out, 'wb').write(body + trailer)
```

验证：`zlib.crc32(data[:-16]) ^ 0xFFFFFFFF` 必须等于文件里的 crc 字段。

### ⚠️ 交付时务必说明

`.img`（解压后）**不能用于网页升级** —— 元数据在 gzip 之外，一解压就没了。
`.img` 只给 Rufus / RKDevTool 线刷；网页升级与 balenaEtcher 烧卡都用 `.img.gz`。

用户报 `Image metadata not present` 时，**第一句先问：你是不是把 .gz 解压了？**

---

## 7. 验证清单（缺一不可）

1. **superblock 对比**：新旧 `inodes` / `block_size` / `compression` 必须一致。
2. **回读目标文件**：`rdsquashfs -c /bin/config_generate <new.sqfs>` 确认已改，且旧值消失。
3. **逐文件 sha256 比对**（最强）：对新 squashfs 再跑 `sqfs2tar`，用 Python `tarfile`
   遍历两边，比 `sha256 + mode + uid + gid + linkname + devmajor/minor`。
   **期望：条目数相同、only-in-A = 0、only-in-B = 0、差异 = 0**（除你改的那一个文件）。
   这一次比对就足以证明重建 100% 无损。
4. **端到端**：对交付的 `.img.gz` 重新解压 → 解析分区表 → 提取 p2 → 与 `new.squashfs`
   比 sha256 → 读出 `config_generate` 复核。
5. **元数据**：`zlib.crc32(final[:-16]) ^ 0xFFFFFFFF` 等于文件里的 crc 字段。
6. 把成品 sha256 给用户核对。

---

## 8. 交付后要提醒用户的事

- **刷机前把同 IP 的旧设备断电**，否则新旧两台抢地址。
- NanoPi R2S 默认网口映射：**eth1 = LAN**（USB 转的那个）、**eth0 = WAN**。
- 全新镜像的 `/etc/config/dhcp` 里 `config dhcp lan` **没有 `option ignore '1'`**，
  即开机就会在 LAN 上跑 dnsmasq DHCP。做旁路由时要手动关掉，否则和主路由抢着发地址。
- 首次启动等 1~2 分钟再访问 `http://<新IP>`。
- **跨大版本升级**（如 iStoreOS 24.10 → 25.12，包管理器 `opkg` → `apk`）官方不支持
  保留配置升级，升级时要选**不保留配置**，新系统的默认值才会生效。
- 官方可能**不推送**大版本 OTA：判断依据是看固件目录里的 `version.latest`
  指向哪个版本，而不是看站点上的最新日期。
