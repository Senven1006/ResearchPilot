# 阿里云部署指南（学生机 / 轻量应用服务器）

学生认证的 300 元券足够跑几个月：学生机常见规格 2 核 2G 约 9.9–30 元/月，具体抵扣以控制台结算页为准。本指南从买机器到拿到公网地址，全部是可复制的命令。

> 目标地址形态：`http://<公网IP>:8000` —— **不需要域名、不需要备案**。只有"用域名访问"时才需要备案。

---

## 0. 买哪台

| 选择 | 建议 |
|---|---|
| 规格 | **2 核 2G 起步**；能选 2 核 4G 更好（构建更快、不会 OOM） |
| 系统镜像 | **Ubuntu 22.04 LTS**（Docker 与依赖支持最好） |
| 产品 | 轻量应用服务器（控制台简单、自带防火墙页面）或 ECS 共享型都行 |
| 地域 | 离你近的（华东/华北），评委访问影响不大 |

## 1. 用券购买

1. 阿里云 → 搜索「学生优惠 / 高校计划」→ 完成学生认证
2. 领券后在「轻量应用服务器」或「云服务器 ECS」下单，**计费方式选包月/包年**（券通常只抵扣新购包周期）
3. 结算页确认券已选中；到期时间建议覆盖评审期（≥1 个月）
4. 创建完成后记下 **公网 IP**。如果创建时要求设置密码，记下来（或后续在控制台重置）

## 2. 放行 8000 端口（最容易忘的一步）

- **轻量应用服务器**：控制台 → 服务器详情 → **防火墙** → 添加规则：协议 TCP、端口 `8000`、来源 `0.0.0.0/0`
- **ECS**：控制台 → 实例 → 安全组 → 配置规则 → 入方向 → 添加：TCP 8000，授权对象 `0.0.0.0/0`

没放行的话，服务起来了但外面访问会一直卡住。

## 3. 连上服务器

控制台「远程连接 / Workbench」直接在网页里开终端，或本地：

```powershell
ssh root@<公网IP>
```

## 4. 加 2G 交换分区（2G 内存必做）

`pip install chromadb / onnxruntime` 与 `npm ci` 在 2G 内存下容易 OOM 被杀，加 swap 最省事：

```bash
fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

## 5. 装 Docker（用阿里云镜像加速）

```bash
apt-get update -y
apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 国内拉基础镜像更快（可选）
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://docker.mirrors.ustc.edu.cn"] }
EOF
systemctl restart docker
```

## 6. 拉代码并构建

```bash
cd /opt
git clone https://github.com/Senven1006/ResearchPilot.git
cd ResearchPilot
docker build -t researchpilot .
```

构建约 5–10 分钟（主要花在 `pip install` 与 `npm ci`）。**若内存不足被 kill**，见第 10 节。

> 如果服务器访问 GitHub 慢/失败：本地把仓库压成 zip 上传（控制台有文件上传），或 `git clone https://gitee.com/...` 镜像后同步。

## 7. 启动服务

```bash
docker run -d --name researchpilot \
  --restart unless-stopped \
  -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxx \
  -e DEMO_SEED=1 \
  -e MAX_PAGES=200 \
  -v researchpilot-data:/data \
  researchpilot

# 等 10 秒后自检
docker logs --tail 30 researchpilot
curl -s http://127.0.0.1:8000/health
```

`-e DEEPSEEK_API_KEY` 记得换成你自己的；`MAX_PAGES=200` 是按 2G 内存给的保守值（本地 541 页压测峰值 664 MB，2G 机器别开太大）。

浏览器打开：

```
http://<公网IP>:8000
```

看到界面、右上角「后端已连接」、论文列表里有示例论文（`DEMO_SEED=1` 自动灌入）即部署成功。

## 8. 自检清单（换手机热点再验一次）

1. `http://<IP>:8000/` → 界面能开
2. `http://<IP>:8000/health` → JSON，`"backend":"hash"`（服务器没装 Ollama，自动降级属正常）
3. 提一个问题 → 有证据卡片与页码
4. 「写作辅助 → 数据图表」上传 `samples/training-log.csv` → 三张图 + 图注
5. 手机 4G 打开同样的地址

## 9. 日常维护

```bash
# 看状态
docker ps --filter name=researchpilot
docker logs --tail 100 researchpilot

# 更新代码（改了仓库之后）
cd /opt/ResearchPilot && git pull
docker build -t researchpilot . && \
docker rm -f researchpilot && \
docker run -d --name researchpilot --restart unless-stopped -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxx -e DEMO_SEED=1 -e MAX_PAGES=200 \
  -v researchpilot-data:/data researchpilot

# 清构建缓存（磁盘吃紧时）
docker system prune -af
```

`--restart unless-stopped`：服务器重启后容器会自动起来，不需要你手动开机。

## 10. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 浏览器一直转圈 / 拒绝连接 | 安全组/防火墙没放行 8000 | 回第 2 步 |
| `docker build` 中途被 kill | 2G 内存 + 无 swap | 按第 4 步加 swap，或先 `docker build` 前 `swapoff` 后重试 |
| 容器起来又退出 | 缺依赖 / 端口占用 | `docker logs researchpilot` 看报错 |
| `/health` 正常但页面 404 | 前端产物没进镜像 | 确认构建上下文包含 `frontend/`，且 `.dockerignore` 没误排 `frontend/src` |
| 问答返回"未配置在线模型" | `DEEPSEEK_API_KEY` 没传或余额不足 | 重新 `docker run` 带上 Key |
| 重启后论文没了 | 没挂 `-v researchpilot-data:/data` | 重新加卷启动；`DEMO_SEED=1` 会重灌示例论文 |
| 上传大 PDF 返回 413 | `MAX_PAGES` 限制 | 调到 200–300（注意内存），或换小文档 |

## 11. 想用域名 / 80 端口（可选，非必需）

- 只是想让地址好看：`docker run -p 80:8000`，然后访问 `http://<公网IP>`（**用 IP 不需要备案**）
- 想用自己的域名：把域名解析到服务器 IP，再装 Nginx 反代到 8000；**境内服务器用域名必须完成 ICP 备案**，否则 80/443 会被拦。评审时间紧就别折腾域名。

## 12. 成本与额度

- 学生机 2 核 2G：常见 9.9–30 元/月（以控制台为准），300 元券通常能覆盖数月到半年
- 流量：轻量套餐一般含月流量（如 1–2 TB），演示用不完
- 用完不续费会自动到期停机，不影响已有作品材料

---

## 部署完成后请做三件事

1. **把地址发我**：`http://<公网IP>:8000`，我同步到 `README.md`、`docs/project-intro.md`（重新生成作品介绍 PDF）与提交包 `00-先读我.md`
2. 填表单第 4 项「在线演示地址」
3. 评审期间每天打开一次确认可用（`curl http://<IP>:8000/health` 也可以）
