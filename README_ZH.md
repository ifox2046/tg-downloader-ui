# tg-downloader-ui

简体中文 | [English](README.md)

基于 [`iyear/tdl`](https://github.com/iyear/tdl) 的轻量级 Telegram 下载 Web UI 与自动化层。

`tdl` 是实际的下载运行时。本项目提供简洁的 Web 控制台、任务历史、路径与来源设置，以及可选的 Telegram 转发器。本项目与 Telegram 无关联，也不是 `tdl` 官方项目。

本服务以本地优先为原则。Python 应用默认仅监听回环地址，Docker Compose 默认也只发布到 `127.0.0.1`。请勿将服务直接暴露到公网；需要远程访问时，请使用可信的 HTTPS 反向代理或 VPN。

## 运行模式

- 基础下载模式：安装并登录 `tdl`，配置下载目录和来源会话，然后在 Web UI 中提交 Telegram 消息 ID。
- 可选转发模式：额外配置你自己的 Telegram API 凭据、Telethon 会话、来源用户或机器人，以及目标频道。

只有在希望本服务监听消息并将摘要转发到你自己的频道时，才需要启用转发器。

## 推荐使用流程（Bot → 自建频道 → 消息 ID 下载）

这是本项目最常见的端到端用法：

```text
来源 Bot
    │  （转发器监听）
    ▼
你的私有频道  ── 展示文件摘要 + 消息ID ──► 复制 ID
    │
    ▼
Web UI：选择来源 + 粘贴消息 ID
    │  （tdl 从来源会话下载）
    ▼
下载目录（Docker 内一般为 /downloads）
```

### 1. 部署与首次初始化

1. 启动 Docker / 安装包后打开 Web UI。
2. 创建管理员账户，并设置**绝对路径**下载目录（Docker 通常填 `/downloads`）。
3. 若希望文件落到 NAS 共享目录，请把宿主机目录 bind 到容器 `/downloads`  
   （例如 `/vol1/.../telegram_downloads:/downloads`）。

### 2. 配置下载来源

在「来源设置」中为每个要下载的 Bot/会话添加条目：

| 字段 | 含义 |
| --- | --- |
| 显示名称 | UI 里显示的标签 |
| Chat | `tdl` 下载用的会话标识（通常是不带 `@` 的 bot 用户名） |
| 转发来源 | Telethon 监听用的 `from_users`（通常是 `@BotUsername`） |

只启用你会用到的来源。同一份来源列表同时服务于手动下载和转发器。

### 3. 登录 `tdl`（下载必需）

`tdl` 才是真正的下载器。请在「tdl 下载登录」中用**能访问来源 Bot 的 Telegram 账号**完成二维码或验证码登录。未完成此项时，即使 Web UI 正常，消息 ID 下载也会失败。

### 4. 推荐：自建私有频道 + 转发器

当你不想在 Bot 会话里手工翻消息 ID 时使用：

1. 创建一个你自己控制的**私有频道**，作为接收摘要的收件箱。
2. 在 https://my.telegram.org 申请 `api_id` / `api_hash`。
3. 打开「Telegram 授权」页，填写 API 凭据、可选代理、会话文件路径（Docker 默认 `/tdl/session.txt`）和频道数字 ID。
4. 用短信验证码或二维码完成 Telethon 授权。该会话与 `tdl` 登录**相互独立**。
5. 保持 `TGDL_FORWARDER_ENABLED=1`（Docker 默认开启）。转发器会监听已启用来源，把含视频的消息摘要发到你的频道。

转发摘要大致包含原始文案，以及类似：

```text
文件: example.mp4
大小: 1.2 GiB
消息ID: 26933
```

把其中的 **消息ID** 复制到 Web UI 下载表单，并选择对应来源；服务会用 `tdl` 按该来源会话 + 消息 ID 下载。

### 5. 提交下载

1. 在首页选择来源 Bot/会话。
2. 粘贴一个或多个消息 ID（来自频道摘要，或直接从 Telegram 取得）。
3. 提交任务，在历史记录中查看进度。
4. 完成后文件出现在配置的下载目录；下载中可能先以 `*.tmp` 存在，完成后再变为最终文件名。

### 必需项与可选项

| 能力 | 需要准备 |
| --- | --- |
| 仅手动消息 ID 下载 | Web UI 管理员 + `tdl` 登录 + 来源 Chat + 下载目录 |
| Bot 监听 → 频道摘要 → 复制 ID 下载 | 以上全部 + Telethon 会话 + 私有频道 ID + 启用转发器 |

若你已经知道消息 ID，可以不启用转发器。  
若只做 `tdl` 下载，不需要 Telegram API 凭据。

## 暂停与恢复语义

暂停和继续是 Linux 上的在线进程控制：服务向同一个正在运行的 `tdl` 进程发送 `SIGSTOP` 和 `SIGCONT`，从而保留 PID、已打开的临时文件和当前字节偏移。取消任务或关闭服务时，会先继续已停止的子进程，再将其正常终止。

这与应用或容器重启后的恢复不同。`tdl 0.20.3 --continue` 可以在包含多个项目的导出文件中跳过已经完整下载的项目，但不能恢复单个未完成文件内部的字节范围。因此，应用或容器重启后，当前单文件仍可能从零开始下载。

## Docker 快速开始

Docker 镜像会在镜像内安装未经修改的 `tdl` 官方发行版二进制文件。AGPL-3.0 声明请参阅 [THIRD_PARTY.md](THIRD_PARTY.md)。

```sh
cp .env.example .env
docker compose up --build
```

打开：

```text
http://localhost:9910
```

首次启动时，初始化页面要求填写：

- 管理员用户名
- 至少八个字符的管理员密码，不限制字符组合
- 绝对下载目录；Docker 通常使用 `/downloads`

第一个完成该表单的浏览器会创建管理员账户。在把监听或发布地址从回环地址改为局域网地址之前，请先完成首次管理员初始化。

如需无人值守初始化，请在首次启动前同时设置 `TGDL_AUTH_USER` 和 `TGDL_AUTH_PASSWORD`。如果希望使用浏览器初始化流程，请不要设置 `TGDL_AUTH_PASSWORD`。真实密码应保存在未被 Git 跟踪的本地环境文件中。

Docker 持久化路径：

- `./data/config` -> `/config`
- `./data/tdl` -> `/tdl`
- `./downloads` -> `/downloads`

这些宿主机目录必须允许 UID/GID `1000` 写入。容器准备好挂载根目录后，会以该非 root 用户运行应用。

发布的 Docker 镜像为多架构（`linux/amd64` 与 `linux/arm64`），在 Docker Hub 上共用同一镜像名与标签：

```text
ifox2046/tg-downloader-ui:0.1.2
ifox2046/tg-downloader-ui:latest
```

各平台在构建时安装对应架构、经校验的上游未修改 `tdl 0.20.3` 二进制（amd64 为 `tdl_Linux_64bit.tar.gz`，arm64 为 `tdl_Linux_arm64.tar.gz`）。在 amd64 主机上本地 `docker compose build` / 普通 `docker build` 仍可直接使用，无需 Buildx。

```sh
docker pull ifox2046/tg-downloader-ui:0.1.2
```

容器默认启动 Web UI 和可选转发器；设置 `TGDL_FORWARDER_ENABLED=0` 可关闭转发器。转发器重启按钮只会重启容器内的转发进程，不需要访问 Docker socket。

多架构镜像仅在版本 tag 或 GitHub Release 时由 Actions 推送（工作流 `Docker Publish`）。PR/main 的 CI 会构建 amd64 与 arm64 以校验 Dockerfile，但不会推送。推送需要仓库密钥 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`，切勿写入本仓库。

## tdl 登录

基础下载模式需要有效的 `tdl` 登录状态。在 Docker 中，优先使用 Web UI：

1. 登录 `http://localhost:9910`。
2. 打开 Telegram 授权页面。
3. 在“tdl 下载登录”中启动二维码登录，并使用自己的 Telegram 账户扫描终端二维码。

需要仅通过命令行检查时，也可以直接运行等效命令：

```sh
docker compose run --rm web tdl login --storage type=bolt,path=/tdl/data
```

非 Docker 安装请按照 `tdl` 上游文档安装，然后使用与 `TGDL_TDL_STORAGE` 配置相同的存储路径执行等效登录命令。

## Telegram API 凭据

只有可选转发器需要 Telegram API 凭据。

1. 使用自己的 Telegram 账户登录 https://my.telegram.org。
2. 创建应用并复制 `api_id` 和 `api_hash`。
3. 创建用于接收转发消息的 Telegram 频道。
4. 将自己的账户加入该频道，并获取频道数字 ID。
5. 设置 `TGDL_API_ID`、`TGDL_API_HASH`、`TGDL_SESSION_FILE` 和 `TGDL_FORWARD_CHANNEL_ID`。

首次初始化完成后，Web UI 会提供“Telegram 授权”页面。可在其中保存 API ID/Hash、会话文件路径、目标频道 ID 和可选代理，然后通过短信验证码或二维码授权 Telethon 账户。UI 会把 Telethon `StringSession` 写入 `TGDL_SESSION_FILE`。Docker 转发器可以复用同一份 `/config/config.json` 和 `/tdl/session.txt` 状态。

`tdl` 登录与 Telethon 授权彼此独立。基础下载需要 `tdl` 登录；转发器需要 Telethon 会话。

请勿公开私有账户的 `api_hash`、会话字符串或频道 ID。

整个状态目录、Telegram 会话、代理凭据、任务日志和下载器日志都应视为敏感数据。不要将它们提交到 Git、附加到 Issue 或放入公开支持日志。

## 配置

配置优先级：

1. 命令行参数（如果对应功能提供）
2. 环境变量
3. `config.json`
4. 安全默认值

`config.json` 位于 `TGDL_STATE_DIR` 下；Docker 中为 `/config`。

| 名称 | 是否必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TGDL_HOST` | 否 | `127.0.0.1` | Web UI 监听地址。Docker 会覆盖容器内监听地址，而 Compose 仍将宿主机发布限制在回环地址。 |
| `TGDL_PORT` | 否 | `9910` | Web UI 端口。 |
| `TGDL_STATE_DIR` | 否 | 用户状态目录 | 配置、数据库、日志和转发器状态。 |
| `TGDL_DOWNLOAD_DIR` | 初始化时 | 用户下载目录 | 默认下载目录。 |
| `TGDL_TDL_BIN` | 否 | `tdl` | `tdl` 二进制文件路径。 |
| `TGDL_TDL_STORAGE` | 否 | 状态目录内的 Bolt DB | `tdl --storage` 参数值。 |
| `TGDL_TDL_LOG` | 否 | 状态目录内日志 | 用于读取 `tdl` 诊断信息的日志路径。 |
| `TGDL_PROXY` | 否 | 空 | 全局代理回退值。 |
| `TGDL_TDL_PROXY` | 否 | `TGDL_PROXY` | `tdl` 下载和导出命令使用的代理；空值表示禁用。 |
| `TGDL_TELEGRAM_PROXY` | 否 | `TGDL_PROXY` | 转发器与 Telethon 使用的代理；空值表示禁用。 |
| `TGDL_SESSION_MAX_AGE` | 否 | `604800` | 登录 Cookie 有效期，单位为秒。 |
| `TGDL_AUTH_USER` | 无人值守初始化 | `admin` | 首次启动前设置 `TGDL_AUTH_PASSWORD` 时使用的管理员用户名。 |
| `TGDL_AUTH_PASSWORD` | 无人值守初始化 | 空 | 首次启动时可选的管理员密码；未设置时使用浏览器初始化页面。不要将此值提交到 Git。 |
| `TGDL_COOKIE_SECURE` | 否 | `0` | 浏览器通过 HTTPS 访问服务时设为 `1`。 |
| `TGDL_PUBLISH_HOST` | Docker | `127.0.0.1` | Docker Compose 发布端口时使用的宿主机地址。 |
| `TGDL_PUBLISH_PORT` | Docker | `9910` | Docker Compose 使用的宿主机端口。 |
| `TGDL_FORWARDER_ENABLED` | 否 | 打包部署中为 `1` | Docker 和 OpenWrt 默认启动可选转发器；设为 `0` 可关闭。 |
| `TGDL_API_ID` | 转发器 | 空 | 来自 `my.telegram.org` 的 Telegram API ID。 |
| `TGDL_API_HASH` | 转发器 | 空 | 来自 `my.telegram.org` 的 Telegram API Hash。 |
| `TGDL_SESSION_FILE` | 转发器 | 状态目录内会话路径 | Telethon 字符串会话文件。 |
| `TGDL_FORWARD_SOURCE` | 回退配置 | 空 | 未配置来源时使用的来源用户或机器人。 |
| `TGDL_FORWARD_CHANNEL_ID` | 转发器 | 空 | 接收转发消息的目标频道 ID。 |
| `TGDL_FORWARDER_LOG` | 否 | 状态目录内日志 | 转发器日志路径。 |
| `TGDL_FORWARDER_STATUS` | 否 | 状态目录内 JSON | 转发器状态 JSON 路径。 |
| `TGDL_FORWARDER_RESTART_CMD` | 否 | OpenWrt 自动检测；Docker 设置本地重启脚本 | Web UI 转发器重启按钮使用的自定义命令。命令会解析为 argv，绝不会通过 shell 执行。 |

代理值使用 URL 格式，例如：

```text
socks5://127.0.0.1:1080
http://127.0.0.1:8080
```

## Python 包

```sh
python -m pip install .
tg-downloader-ui
```

安装包包含 Telegram 授权所需的 Telethon、qrcode，以及代理库（`python-socks`、`PySocks`）。转发器仍为可选功能：

```sh
tg-downloader-forwarder
```

## OpenWrt / iStoreOS

在普通开发机上构建 OpenWrt `.ipk` 包：

```sh
python scripts/build_openwrt_ipk.py
```

默认构建会生成三个安装包：

- `tg-downloader-ui_0.1.0_all.ipk`：与架构无关的应用包，需要另行安装与设备架构匹配的上游 `tdl`。
- `tg-downloader-ui-full_0.1.0_x86_64.ipk`：x86_64 完整包，包含应用和未经修改的上游 `tdl 0.20.3` 二进制文件（`tdl_Linux_64bit.tar.gz`）。
- `app-meta-tg-downloader-ui_0.1.0-r1_all.ipk`：iStore 已安装应用元数据包。

构建独立的 aarch64 完整包（OpenWrt `Architecture: aarch64_generic`，上游 `tdl_Linux_arm64.tar.gz`）：

```sh
python scripts/build_openwrt_ipk.py --full-arch aarch64
# 或同时构建全部 full 架构：
python scripts/build_openwrt_ipk.py --full-arch all
```

使用 `--full-arch all` 时还会生成 `tg-downloader-ui-full_0.1.0_aarch64_generic.ipk`。不同 CPU 架构的 full 包是独立 IPK 文件，包名同为 `tg-downloader-ui-full`，并 Conflicts/Provides `tg-downloader-ui`。

两个应用包（generic 与任一 full）只能选择一个安装：

```sh
opkg install tg-downloader-ui_0.1.0_all.ipk
# 或者在 x86_64 设备上：
opkg install tg-downloader-ui-full_0.1.0_x86_64.ipk
# 或者在 aarch64 OpenWrt 上：
opkg install tg-downloader-ui-full_0.1.0_aarch64_generic.ipk
```

generic 与 full 包拥有相同的运行时文件，因此声明为互相冲突。full 包省去了单独安装 `tdl` 的步骤，但仍需完成首次管理员初始化，并通过 Web UI 二维码流程或使用相同存储路径执行 `tdl login` 登录自己的 Telegram 账户。

安装包包含 Web 应用、procd 初始化脚本、环境变量模板和 LuCI 菜单入口。Telethon、qrcode、rsa、pyasn1、pyaes 以及所需代理依赖会经过校验并预置在 IPK 中，因此路由器安装时不会运行 `pip`，可离线完成。

完整手工测试清单请参阅 [docs/TESTING.md](docs/TESTING.md)，OpenWrt/iStoreOS 真机测试清单请参阅 [docs/OPENWRT_TESTING.md](docs/OPENWRT_TESTING.md)。

使用环境变量模板：

```sh
cp openwrt/tg-downloader-ui.env.example /etc/tg-downloader-ui.env
chmod 600 /etc/tg-downloader-ui.env
```

编辑 `/etc/tg-downloader-ui.env` 后重启：

```sh
/etc/init.d/tg-downloader-ui restart
```

Docker 和 OpenWrt 打包部署默认启动可选转发器。在 OpenWrt 上设置 `TGDL_FORWARDER_ENABLED=0` 可关闭转发器。

## 开发

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
python scripts/build_openwrt_ipk.py
```

## 许可证

本项目自身代码采用 MIT 许可证。多架构 Docker 镜像（`linux/amd64` 与 `linux/arm64`）以及 full OpenWrt IPK（`tg-downloader-ui-full_0.1.0_x86_64.ipk` 与 `tg-downloader-ui-full_0.1.0_aarch64_generic.ipk`）内置的 `tdl` 是未经修改的上游 `tdl 0.20.3` 二进制文件，采用 AGPL-3.0 许可证。每个 full IPK 会在 `/usr/share/licenses/tg-downloader-ui-full` 中安装上游许可证及源码/版本声明。详情参阅 [THIRD_PARTY.md](THIRD_PARTY.md)。
