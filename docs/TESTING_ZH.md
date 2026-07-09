# 手动测试指南

本指南覆盖 `tg-downloader-ui` 在 Docker、Python 包安装、OpenWRT 以及可选的 Telegram 转发器模式下的完整手动测试。

请使用你自己的 Telegram 账号和测试频道。不要将真实的 `api_hash`、会话字符串、cookies、私有频道 ID 或私有路径粘贴到 issue、截图或公开日志中。

## 1. CI 已覆盖的内容

CI 和本地自动化测试覆盖了以下内容：

- 元数据解析
- 任务的创建、暂停、恢复、重试、删除
- 认证会话行为
- 初始化要求行为
- 下载目录校验
- 源配置
- 代理优先级
- 转发器配置校验
- 发布安全性扫描（私有默认值检查）
- Python 包构建
- Docker 镜像构建和 `tdl version` 冒烟测试

手动测试应重点关注真实运行时行为：

- 浏览器中的首次运行初始化
- 真实的 `tdl` QR 登录
- 真实的 Telegram 消息下载
- 基于真实未完成下载的暂停/恢复
- 数据卷持久化
- 可选转发器（使用真实 Telegram API 凭证）
- 平台特定部署，尤其是 OpenWRT init/LuCI

## 2. 所需测试数据

测试前请准备：

- 一个由你控制的 Telegram 账号。
- 一个包含至少一个可下载文件的源聊天/频道/机器人。
- 该源中一个已知的消息 ID。
- 足够的磁盘空间存放测试文件。
- 可选：一个你拥有的 Telegram 频道，用于转发器测试。
- 可选：从 https://my.telegram.org 获取的 `api_id` 和 `api_hash`，用于转发器。

推荐的测试文件：

- 足够小以便快速完成。
- 足够大以便在需要测试取消/重试时可以观察到进度。
- 非敏感文件，因为文件名和消息文本可能出现在日志中。

## 3. Docker 测试

Docker 是推荐的通用服务器/预发布环境方式。镜像安装的是未修改的 `iyear/tdl` 发布版二进制文件。请参阅 `THIRD_PARTY.md` 了解 AGPL-3.0 许可声明。

### 3.1 从干净的 Docker 状态开始

在仓库根目录下执行：

```sh
docker compose down --remove-orphans
rm -rf data downloads
cp .env.example .env
```

如果你的网络需要代理，编辑 `.env`：

```sh
TGDL_PROXY=socks5://127.0.0.1:1080
TGDL_TDL_PROXY=
TGDL_TELEGRAM_PROXY=
```

启动 Web 服务：

```sh
docker compose up --build
```

预期结果：

- 镜像构建成功。
- 容器启动不崩溃。
- 端口 `9910` 对外开放。

打开：

```text
http://localhost:9910
```

预期结果：

- 首次访问会打开 `/setup` 页面。
- 在初始化之前，`/login` 会重定向到 `/setup`。
- 没有可用的默认管理员密码。

### 3.2 首次运行初始化

填写初始化表单：

- 管理员用户名：任意测试用户名。
- 管理员密码：任意强密码。
- 下载目录：`/downloads`。
- 转发器字段：留空，仅做基础下载测试。

提交。

预期结果：

- 浏览器跳转到 `/login`。
- 使用新管理员账号登录成功。
- `./data/config/config.json` 文件存在。
- `config.json` 不包含明文密码。

检查：

```sh
grep -n "password" ./data/config/config.json
```

预期结果：可能存在密码哈希/盐值字段；明文密码必须不存在。

### 3.3 Docker 中的 tdl 登录

优先使用 Web UI：

1. 登录应用。
2. 打开 Telegram 授权区域。
3. 在 `tdl 下载登录` 中启动 QR 登录。
4. 使用你自己的 Telegram 账号扫描页面显示的终端 QR 输出。

预期结果：

- QR 输出显示在一个固定区域，重新发起登录时会替换旧内容。
- 登录最终报告成功。
- 项目文件中不打印会话字符串。

仅需要命令行检查时，可使用：

```sh
docker compose run --rm web tdl login --storage type=bolt,path=/tdl/data
```

对 `tdl` 做冒烟检查：

```sh
docker compose run --rm web tdl --storage type=bolt,path=/tdl/data chat ls
```

预期结果：

- 列出你可访问的聊天列表。
- 项目文件中不打印会话字符串。

### 3.4 配置下载源

在 Web UI 中：

1. 打开源配置页面。
2. 添加一个你的 `tdl` 账号可以访问的源。
3. 填写：
   - 标签：任意易读的名称
   - `tdl` 聊天：`tdl` 使用的源聊天用户名或标识符
   - 转发源：可选的 `@username`
   - 启用：勾选
   - 默认：选中
4. 保存。

预期结果：

- 刷新页面后源配置仍然保留。
- 下载页面的源下拉菜单显示该源。

### 3.5 基础下载

从已配置的源中提交已知的消息 ID。

预期的任务流程：

```text
排队中 -> 导出中 -> 下载中 -> 完成
```

检查文件：

```sh
find ./downloads -type f | head
```

检查日志：

```sh
find ./data/config/logs -type f -maxdepth 1 -print
tail -n 80 ./data/config/logs/*.log
```

通过标准：

- 文件出现在 `./downloads` 目录下。
- 任务状态为 `done`（完成），如果最终文件已存在则为 `skipped`（跳过）。
- 任务日志包含 `tdl` 导出/下载输出。
- Web UI 不暴露 `api_hash` 或会话字符串。

### 3.6 暂停、恢复、重试、删除

暂停：

1. 提交一个较大的测试文件。
2. 在任务进行中点击暂停。

预期结果：

- 进行中的任务记录暂停请求。
- 持久化状态可能仍是 `canceled`，但 UI 应以暂停/可恢复的操作语义展示。

恢复：

1. 恢复已暂停的任务。
2. 查看任务日志。

预期结果：

- 尝试次数只按恢复运行的预期增加。
- 恢复时保留已有进度和已下载大小字段。
- 恢复命令同时包含 `tdl download --continue` 和 `-f <export.json>`。

重试：

1. 重试一个失败且不可恢复的任务。

预期结果：

- 尝试次数增加。
- 任务回到 `queued`（排队中）状态并走普通下载流程。

删除：

1. 删除一个已完成的任务。

预期结果：

- 任务行消失。
- 任务导出/日志的相关文件被移除。

### 3.7 Docker 数据持久化

重启：

```sh
docker compose down
docker compose up
```

预期结果：

- 登录仍然有效。
- 源配置持久保留。
- 下载目录持久保留。
- 任务历史持久保留。
- 已下载的文件仍保留在 `./downloads` 下。

### 3.8 Docker 可选转发器

如果只需基础下载模式，可跳过此部分。

你可以在 Web UI 中保存 Telegram API 设置并完成 Telethon 授权，也可以在启动转发器前准备 `.env`：

```sh
TGDL_API_ID=your_api_id
TGDL_API_HASH=your_api_hash
TGDL_SESSION_FILE=/tdl/session.txt
TGDL_FORWARD_CHANNEL_ID=-100your_channel_id
TGDL_PROXY=
TGDL_TELEGRAM_PROXY=
```

创建或提供转发器所需的会话文件。Web UI 可以把 Telethon `StringSession` 写入 `/tdl/session.txt`；如果环境变量为空，转发器会回退读取 `/config/config.json`。

启动 Docker：

```sh
docker compose up
```

预期结果：

- 转发器服务不因配置错误而退出。
- Web UI 转发器状态从缺失/过期变为运行中。
- 从已配置源发送的测试消息会在目标频道中生成转发摘要。
- 修改来源配置后，先重启转发器再测试新增来源。随仓库提供的
  Docker 镜像会在同一容器内运行 Web UI 和 `tg-downloader-forwarder`，
  Web UI 的重启按钮会重启容器内的 forwarder 进程。

如果失败：

```sh
docker compose logs web
cat ./data/config/forwarder_status.json
tail -n 100 ./data/config/forwarder.log
```

常见的预期错误：

- `TGDL_API_ID is required`
- `TGDL_API_HASH is required`
- `TGDL_FORWARD_CHANNEL_ID is required`
- `session file not found`
- 代理连接失败

### 3.9 Docker 清理

停止：

```sh
docker compose down --remove-orphans
```

仅在不再需要时删除运行时数据：

```sh
rm -rf data downloads
```

## 4. Python 包测试

使用此方式验证非 Docker 服务器安装。

创建虚拟环境：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip build
python -m build
python -m pip install dist/*.whl
```

冒烟检查命令：

```sh
tg-downloader-ui --check
tg-downloader-forwarder
```

预期结果：

- `tg-downloader-ui --check` 输出 `ok`。
- `tg-downloader-forwarder` 在未配置转发器凭证时，以明确的配置错误退出。

运行 Web UI：

```sh
TGDL_STATE_DIR=$(pwd)/data/config \
TGDL_DOWNLOAD_DIR=$(pwd)/downloads \
TGDL_TDL_STORAGE=type=bolt,path=$(pwd)/data/tdl/data \
tg-downloader-ui --host 0.0.0.0 --port 9910
```

然后使用本地路径（而非 Docker 数据卷）重复 Docker 部分的浏览器、`tdl` QR 登录、源配置、下载、暂停/恢复和持久化测试。

## 5. OpenWRT 测试

OpenWRT 有自己完整的真机检查清单：

[OPENWRT_TESTING.md](OPENWRT_TESTING.md)

使用它来验证：

- 原生 init 脚本
- `/etc/tg-downloader-ui.env`
- LuCI 菜单链接
- 路由器存储路径
- OpenWRT 重启行为
- 路由器硬件上的可选转发器

## 6. 发布前的自动化验证

在正常开发机器或预发布主机上运行：

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
docker build --build-arg TDL_VERSION=0.20.3 -t tg-downloader-ui:test .
docker run --rm tg-downloader-ui:test tdl version
docker run --rm tg-downloader-ui:test tg-downloader-ui --check
```

预期结果：

- 所有单元测试通过。
- compileall 成功。
- Python 包构建出 sdist 和 wheel。
- Docker 镜像构建成功。
- 容器报告 `tdl` 版本号。
- 容器应用检查输出 `ok`。

## 7. 最终验收检查清单

- [ ] Docker 从干净的 `data/` 和 `downloads/` 目录启动。
- [ ] 首次运行需要初始化设置。
- [ ] 初始化之前，没有可用的默认管理员密码。
- [ ] 初始化后管理员登录正常。
- [ ] 使用你自己的 Telegram 账号完成 `tdl` QR 登录。
- [ ] 源配置持久保留。
- [ ] 已知消息 ID 下载成功。
- [ ] 暂停、恢复、重试和删除行为正确。
- [ ] 重启后配置、任务和已下载文件均保留。
- [ ] 如果启用了可选转发器，转发功能正常工作。
- [ ] 如果 OpenWRT 是发布目标之一，OpenWRT 检查清单通过。
- [ ] 日志和截图不暴露 Telegram API hash 或会话字符串。
