# 光谷遛娃活动情报 🦖

每周三、周日 19:30 自动扫描武汉光谷一带的公益亲子活动（光谷书房、光谷图书馆/美术馆、湖北省科技馆、中国建筑科技馆），用 Claude 提取活动信息并按「7 岁适龄、光谷范围内、周末、免费/低价」筛选，推送到飞书群机器人。

## 工作原理

```
GitHub Actions 定时任务（云端免费，不用自己开机）
  → 抓取：官网直抓（楚天书房活动预告、省科技馆官网）+ Bing RSS 关键词轮询（覆盖公众号文章和新闻转载）
  → 抓文章正文 → Claude API 提取结构化信息（时间/地点/年龄/费用/报名方式）+ 规则筛选
  → 生成卡片 → 飞书群机器人推送
  → state/seen.json 去重（已处理的链接不再重复推送）
```

## 部署方式一：阿里云服务器（推荐，约 20 分钟）

> 推荐自有服务器而不是 GitHub Actions：服务器在国内，抓省科技馆、公众号文章等国内站点稳定不触发反爬；去重状态直接落盘，无需回写仓库。

**准备**：任意一台 Linux 云服务器（阿里云轻量应用服务器/ECS 均可，最低配足够，Ubuntu 22.04 或同类发行版），已配好安全组能出网。

```bash
# 1. 本机：把项目传到服务器
scp -r wuhan-kids-events root@<服务器IP>:/opt/

# 2. 服务器：装依赖
ssh root@<服务器IP>
cd /opt/wuhan-kids-events
apt update && apt install -y python3 python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. 配置密钥
cp .env.example .env && chmod 600 .env
vi .env   # 填入 FEISHU_WEBHOOK、FEISHU_SECRET；LLM 凭证二选一：
#   - Anthropic 官方：ANTHROPIC_API_KEY
#   - DeepSeek 兼容端点：ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN + ACTIVITY_MODEL

# 4. 首跑：把现有内容标记为已见（避免推一堆旧活动）
.venv/bin/python pipeline.py --init

# 5. 定时任务：阿里云服务器默认北京时间
crontab -e
# 加入这几行：
# 30 19 * * 0,3 /opt/wuhan-kids-events/run.sh            # 周日/周三 19:30 扫描
# 0 8 * * 1 /opt/wuhan-kids-events/run.sh                # 周一 8:00 补扫
# 0 20 * * 0 /opt/wuhan-kids-events/run.sh --hbstm       # 周日 20:00 刷新省科技馆当月日程
# 50 11 * * 1 /opt/wuhan-kids-events/run.sh --remind     # 周一 11:50 线上抢课
# 30 8 * * * /opt/wuhan-kids-events/run.sh --remind      # 每天 8:30 现场取票提醒
```

- 周一 8:00 那次是补扫：如果周日晚上的抓取错过了科技馆周日晚间才发的课表，周一早上的推送仍赶得上**中午 12:00 的抢课**。
- 周日 20:00 读 [活动日程](https://www.hbstm.org.cn/webart/WebArtIdx/117/) 里覆盖本月的公众号长图，识别出场次，推一份月报。文章没换就不重复推。
- 周一 11:50 按这份课表提醒 12:00 线上抢当周课程；每天 8:30 提醒当天要在预约机取票的表演。年龄对不上 `config.json` 里的孩子的场次只写在月报里，不响铃。
- 手动跑一次看效果：`/opt/wuhan-kids-events/run.sh --dry-run` 预览卡片，`/opt/wuhan-kids-events/run.sh` 真实推送；日志在 `logs/scan.log`。
- 换成腾讯云/华为云/家里 NAS 同理，只要机器能上网且常开即可。

## 部署方式二：GitHub Actions（免费，无需服务器）

> 缺点：Actions 跑在境外机房，个别国内站点可能拦截。`state/` 和 `output/` 在 `.gitignore` 里，Actions 每次都是新环境，去重和当月课表不会留到下一次。要稳定去重和预约提醒，用上面的自有服务器。

### 1. 建飞书机器人，拿 webhook

1. 手机/电脑打开飞书，建一个只有你自己的群（或家人群），群名随意
2. 群设置 → 群机器人 → 添加机器人 → 自定义机器人
3. 安全设置选 **「加签」**（推荐）或「自定义关键词」——GitHub 的服务器 IP 会变，不要选 IP 白名单
4. 复制 **Webhook 地址**（`https://open.feishu.cn/open-apis/bot/v2/hook/xxx`）；如果选了加签，把**签名密钥**也复制下来

### 2. 准备 LLM 凭证（二选一）

- **Anthropic 官方**：到 https://console.anthropic.com 生成 API Key，每周提取费用约 ¥1/月
- **DeepSeek 兼容端点**（更便宜）：用 DeepSeek 的 API Key，配置 `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` + `ANTHROPIC_AUTH_TOKEN` + `ACTIVITY_MODEL=deepseek-v4-pro[1m]`，代码会自动跟随环境变量

### 3. 放到 GitHub 上定时跑

1. 注册/登录 GitHub，新建一个 **私有** 仓库（名字随意，如 `guanggu-kids-events`）
2. 把这个目录推上去（本机执行）：

   ```bash
   cd wuhan-kids-events
   git init && git add -A
   git commit -m "光谷遛娃活动情报 v1"
   git remote add origin git@github.com:<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

3. 仓库页面 → Settings → Secrets and variables → Actions → New repository secret，添加三个：

   | 名称 | 值 |
   |---|---|
   | `FEISHU_WEBHOOK` | 第 1 步的 webhook 地址 |
   | `FEISHU_SECRET` | 加签密钥（没选加签就随便填个空字符串） |
   | `ANTHROPIC_API_KEY` | 第 2 步的凭证（用 DeepSeek 则改配 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ACTIVITY_MODEL` 三个 secret，不需要 API Key） |

4. 进 Actions 页启用工作流。第一次请点 **Run workflow** 并勾选 **init**（把现存内容全部标记为已见，避免首跑推一堆旧活动）；然后再点一次 Run workflow（不勾选）验证真实推送。之后每周日/周三 19:30 自动扫描。省科技馆当月课表由 `alarm.yml` 在周日 20:00 整理，周一 11:50 提醒线上抢课，每天 8:30 提醒现场取票

### 4. 按自己家调整配置

改 `config.json`：

- `home.label`：改成你家小区/最近地铁站（AI 用它判断距离）
- `child.age` / `age_min` / `age_max`：孩子年龄范围
- `sources.searches`：可增删场馆和关键词（想加武汉植物园之类，加一组 queries 即可）

## 本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export FEISHU_WEBHOOK=https://open.feishu.cn/...
python pipeline.py --dry-run   # 只打印卡片预览，不推送
python pipeline.py --init      # 首次部署前跑一次：把现有内容标记为已见
python pipeline.py             # 真实推送
python pipeline.py --hbstm --dry-run   # 整理省科技馆当月活动，只打印
python pipeline.py --hbstm             # 识别课表；换了新文章才推月报
python pipeline.py --remind            # 此刻若在提醒窗口内，推预约提醒
```

## 已知限制（重要）

- **微信公众号无法自动监控**。机器能抓「公众号文章链接」，但发现新文章的渠道是搜索轮询和官网转载，时效性有缺口。请每周花 2 分钟扫一眼这几个公众号的报名入口（推送卡片底部也会提醒）：
  - 光谷书房（报名链接一般 19:30 开启，名额 8~15 人）
  - 湖北省科学技术馆（周日 19:01 发下周课表，**周一 12:00 抢课程**）
  - 光谷图书馆、光谷美术馆（每周一放下一周名额）
  - 中国建筑科技馆、地大逸夫博物馆（夜宿/研学招募贴）
- 省科技馆当月课表在活动日程列表页（服务端直接输出，链到公众号长图）。场次画在图片里，脚本用本地文字识别读表，第一次会下载识别模型。图特别花时个别场次名称可能认错，以原文为准。
- 距离判断目前靠 AI 读地点文字，精确的坐标测距（高德 API）留到 v2。
- GitHub Actions 的服务器在境外，个别国内站点可能反爬拦截；本地跑一次可作补充。

## 成本

- GitHub Actions：免费（私有仓库含 2000 分钟/月，本任务每月用几分钟）
- Claude API：每次运行约 ¥0.5~1，每月约 ¥5 以内
- 飞书机器人：免费
