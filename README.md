# Gemini Web2API Debian 12 中文安装脚本

这是一个可单独分享、直接在 **Debian 12 服务器本机运行**的 Bash 安装器。它不依赖 Windows、PowerShell、SSH 别名或本地开发环境。

## 快速开始

将 [`install.sh`](install.sh) 放到 Debian 12 服务器后运行：

```bash
sudo bash install.sh
```

脚本会用中文菜单让你选择安装/更新、预演或查看状态，并交互询问服务端口、是否绑定域名及域名值。选择域名后，需要先将该域名的 A 记录配置到服务器；脚本会使用服务器上已有的 Caddy 配置 HTTPS，不会另装反向代理。安装前会展示配置摘要，输入 `Y` 才开始安装，其他输入都会取消。

也可以直接从 GitHub 下载运行：

```bash
curl -fsSL https://raw.githubusercontent.com/yys9253462-gif/gemini-web2api-sweden-installer/main/install.sh -o install.sh
sudo bash install.sh
```


## 功能

- 仅支持 Debian 12；自动安装 Git、Python 及虚拟环境依赖。
- 从上游 [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api) 克隆或安全快进更新。
- 创建独立系统用户和 systemd 服务；应用仅绑定 `127.0.0.1`，不会公开应用端口。
- 首次安装生成随机 API Key，配置保存在 `/etc/gemini-web2api/config.json`，权限为 `0640`；默认启用临时聊天。
- 域名模式会备份 `/etc/caddy/Caddyfile`、追加站点、校验并热加载；失败会还原原配置。
- 域名模式会在 Caddy 层屏蔽公网 `/v1beta/*`，以规避上游当前原生 API 路由未纳入同一鉴权检查的问题。OpenAI 兼容 `/v1/*` 仍使用配置中的 API Key。
- 安装后验证本机健康接口、loopback 监听，以及 `/v1/models` 无密钥返回 401、带密钥返回 200。
- 如果项目目录已有不同仓库、未提交改动或非快进上游更新，脚本会停止，不覆盖现有源代码。
- 可识别旧版安装失败后留下的服务账户主目录：会先保留到 `/var/lib/gemini-web2api/recovered-old-home-*`，再继续安装，避免把该主目录误判为项目源码。

## 命令

```bash
# 中文交互菜单
sudo bash install.sh

# 交互式预演，不安装软件、不改配置
sudo bash install.sh --dry-run

# 交互式查看状态
sudo bash install.sh --status

# 自动化模式：不显示菜单；未提供域名时仅本机监听
sudo bash install.sh --non-interactive --port 8081

# 自动化模式：绑定已有 DNS 和 Caddy
sudo bash install.sh --non-interactive --domain gemini.example.com --port 8081

# 查看帮助
bash install.sh --help
```

## 安装结果

- 上游代码：`/opt/gemini-web2api`
- 服务配置：`/etc/gemini-web2api/config.json`
- systemd：`gemini-web2api.service`
- 服务端口：默认 `127.0.0.1:8081`
- 日志：`journalctl -u gemini-web2api -n 100 --no-pager`

安装结束时会显示接口密钥，请妥善保存。绑定域名时，API 地址为 `https://域名/v1`。不绑定域名时，服务只在服务器本机开放；远程客户端需自行建立 SSH 隧道，例如：

```bash
ssh -N -L 8081:127.0.0.1:8081 用户名@服务器地址
```

## 前提与限制

- 域名模式要求服务器已安装并运行 Caddy，配置文件为 `/etc/caddy/Caddyfile`，公网 80/443 可用于证书签发。
- 脚本必须以 root 或 sudo 权限运行。
- 上游通过 Gemini Web 内部协议工作，Google 改动网页接口后可能需要更新上游代码。
- 本脚本只负责安装和配置；运行它会实际安装软件并启用服务。请先用 `--dry-run` 查看计划。

MIT 许可证，见 [LICENSE](LICENSE)。
