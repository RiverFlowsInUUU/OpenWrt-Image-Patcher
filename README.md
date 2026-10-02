# OpenWrt / iStoreOS 镜像定制器

把官方刷机镜像里的**默认配置**改成你想要的 —— 刷完开机直接就是目标状态，
省掉「刷完机 → 改电脑网卡 → 进后台 → 改地址 → 电脑网卡改回来」那一圈。

固件镜像不动原始文件、不破坏 U-Boot / 分区表 / 文件权限，网页升级也能直接用。

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![deps](https://img.shields.io/badge/deps-squashfs--tools--ng-lightgrey)

---

## 它改什么

| 场景 | 命令 |
|:-----|:-----|
| 改 iStoreOS 出厂默认 LAN IP（`192.168.100.1` → 你要的地址） | `--lan-ip 192.168.1.1` |
| 任意字符串替换（改 hostname、默认密码、预置 uci 配置…） | `--replace OLD NEW --in-file <路径>` |

以 iStoreOS 25.12 为例，出厂默认地址的真正生效点只有一处：

```sh
# /bin/config_generate
lan) ipad=${ipaddr:-"192.168.100.1"} ;;
```

（`/etc/config/network` 在镜像里根本不存在，是首次启动时由这个脚本生成的。
其余几处 `192.168.100.1` 都只是 LuCI 页面的提示文案，改了没用。）

---

## 快速开始

### 1. 装依赖：squashfs-tools-ng

只需要 `sqfs2tar` / `tar2sqfs` / `rdsquashfs` 三个可执行文件。

**Windows**：下载原生 MinGW 预编译包，解压即用（自带全部 DLL）

```
https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip
```

解压后把 `bin` 目录路径传给 `--tools-dir`。
（目录列表里若有更新版本，直接换新的即可。）

**Linux**：`apt install squashfs-tools-ng` / `dnf install squashfs-tools-ng`
**macOS**：`brew install squashfs-tools-ng`

### 2. 改镜像

```bash
python scripts/patch_firmware.py \
    -i istoreos-25.12.5-2026091113-r2s-squashfs.img.gz \
    -o istoreos-25.12.5-r2s-lan192.168.1.1.img.gz \
    --lan-ip 192.168.1.1 \
    --tools-dir tools/squashfs-tools-ng-1.3.2-mingw64/bin
```

输出两个文件：

| 文件 | 用途 |
|:-----|:-----|
| `*-lan192.168.1.1.img.gz` | 网页升级 / balenaEtcher 烧卡 |
| `*-lan192.168.1.1.img` | Rufus / RKDevTool 线刷 |

### 3. 刷机

- **网页升级**：后台「系统 → 备份/刷写固件」，**直接上传 `.img.gz`，不要解压**
- **烧卡**：balenaEtcher 直接选 `.img.gz`（它会自动解压）
- **线刷**：RKDevTool / Rufus 用解压后的 `.img`

---

## 命令行参数

| 参数 | 说明 |
|:-----|:-----|
| `-i, --input` | 原始 `.img` / `.img.gz`（**不会被修改**） |
| `-o, --output` | 输出路径；`.img.gz` 会自动再落一份同名 `.img` |
| `--lan-ip` | 把 lan 默认 IP 改成该地址 |
| `--replace OLD NEW` | 通用字符串替换，配合 `--in-file` 限定文件 |
| `--in-file` | 只在这个路径内替换，如 `bin/config_generate` |
| `--tools-dir` | squashfs-tools-ng 的 `bin` 目录 |
| `--jobs` | tar2sqfs 并行数（默认 CPU 核数） |
| `--workdir` | 中间文件目录（指定后不会被自动清理） |
| `--no-verify` | 跳过逐文件比对（**不推荐**） |

---

## 它会自动处理的三件事

这几步是手工做最容易踩坑的地方，脚本内置了：

**① 权限不能丢。**
不能用「解包到磁盘再打包」——Windows/NTFS 存不了 Unix mode/uid/gid，
重建出来所有文件权限变默认值，`/sbin/init`、`/etc/init.d/*` 不可执行，系统直接起不来。
脚本走 `sqfs2tar → 内存中改 → tar2sqfs`，全程保留 tar 里的权限与属主。

**② 重建参数必须和原镜像一致。**
压缩算法、块大小、xattr 标志都从原镜像的 squashfs superblock 现读现用，
不靠猜（原镜像用什么就是什么）。

**③ fwtool 元数据段必须补回去。**
`.img.gz` 不只是 gzip，它在 gzip 流**之后**还挂了一段 OpenWrt 的元数据
（`FWx0` magic + crc32），网页升级靠它做设备与版本校验。
重新压缩会把它丢掉，结果就是升级时报：

```
upgrade: Image metadata not present
upgrade: Use sysupgrade -F to override this check ...
Image check failed.
```

脚本会复用原元数据的 header/JSON 并重算 CRC，保证新镜像照样能过校验。

> ⚠️ 正因为元数据挂在 gzip 流**外面**，所以 **`.img` 不能用于网页升级** ——
> 你一把 `.gz` 解压，元数据就没了。遇到上面那个报错，先检查是不是解压后再上传的。

---

## 校验

每次运行结束都会打印结果，任何一项失败都会以非零码退出：

- squashfs superblock 的 inodes / block_size / compression 与原镜像一致
- 新镜像回读（sqfs2tar）后与原 tar **逐文件 sha256 + 权限 + 属主 + 链接 + 设备号**比对，
  期望「仅目标文件不同，其余差异 0」
- fwtool 元数据 CRC 自校验
- 输出 `.gz` 解压后与内存中的 `.img` 逐字节一致

---

## 兼容性

- 理论上适用于任何 squashfs 根文件系统 + MBR/GPT 的 OpenWrt 系镜像；
  分区表和 squashfs 位置是**动态解析**的，不写死。
- 实测通过：**NanoPi R2S + iStoreOS 25.12.5**（`rockchip/armv8`）。
  该机型分区布局：`0~16MB` U-Boot | `p1 @16MB` ext4 64MB | `p2 @84.9MB` squashfs 256MB |
  `p3 @354.4MB` rootfs_data 标记。
- 跨大版本升级（如 iStoreOS 24.10 → 25.12）官方不支持保留配置
  （`opkg` → `apk`），升级时要选**不保留配置**，这样新系统的默认值才会生效。

---

## 作为 AI Agent Skill 使用

本仓库本身就是一个标准的 Agent Skill 包：

```
SKILL.md                     # 给 Agent 读的操作手册（YAML frontmatter + 完整流程与踩坑）
scripts/patch_firmware.py    # 可直接执行的工具
```

把整个目录放进 Agent 的 skill 目录即可（例如
`~/.workbuddy/skills/openwrt-image-default-ip-patch/`），
之后遇到「改固件默认 IP / 刷机镜像定制 / 网页升级报 Image metadata not present」
这类任务时会被自动加载。

`SKILL.md` 里除了操作步骤，还记了四个**必踩的坑**（MSYS 路径转换、
`rdsquashfs -l` 的参数、NTFS 丢权限、fwtool 元数据段丢失）和完整的验证清单 ——
即使不使用脚本、纯手工操作，照着做也不会踩雷。

---

## 相关文档

- [`SKILL.md`](SKILL.md) —— 给 AI Agent 用的操作手册（含格式细节与踩坑记录）

## License

[MIT](LICENSE)
