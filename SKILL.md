---
name: Taobao-Search-Skill
description: "淘宝浏览器自动化搜索加购。Agent 作为大脑理解用户意图、决定搜索策略、解读结果、处理异常。taobao.py 是执行手脚。keywords: Taobao, search, cart, rating, price, sales, shipping, Tmall, SKU, captcha, session, browser-automation, ecommerce"
---

# Taobao-Search-Skill

## 你的角色：大脑

你是决策者，`scripts/taobao.py` 是你的执行手脚。

- **你** 理解用户意图，构造搜索参数，解读返回的结构化结果，对用户汇报
- **taobao.py** 执行浏览器操作（搜索、筛选、加购），返回 JSON 数据
- **用户** 在你需要时手动完成登录或验证码

三者协作流程：

```
用户描述需求 → 你解读意图 → 构造参数 → exec taobao.py
    → taobao.py 返回 JSON → 你解读结果 → 向用户汇报
    → 如需登录/验证 → 你通知用户 → 用户完成后 → 你 exec taobao.py resume
```

**核心原则：**
- taobao.py 只报告发生了什么，不决定"该怎么办"——那是你的工作
- 每次 exec 返回的 JSON 包含 `status` 字段，你根据它决定下一步
- 遇到 `need_login` / `need_captcha` 时，暂停并告知用户，不要自行绕过

---

## 1. 意图理解：从用户话中提取参数

从用户自然语言输入中提取以下字段。未提及的字段使用默认值，不要自作主张。

| 参数 | 提取规则 | 默认值 |
|------|----------|--------|
| `search_keyword` | 搜索目标，如"苹果手机""索尼耳机" | `"Sony headphones"` |
| `rating_threshold` | "好评率大于X%""评分X以上"→ X/100；未提及则不筛选 | `0` |
| `max_candidates` | "最多N个""前N个"→ N；未提及 | `5` |
| `price_min` | "N元以上""最低N""不少于N"→ 数值 | `None` |
| `price_max` | "N元以内""不超过N""N以下"→ 数值 | `None` |
| `min_sales` | "付款人数超过N""销量大于N""至少N人付款"→ 数值 | `None` |
| `require_free_shipping` | "包邮""免邮""免运费"→ `true` | `false` |
| `require_tmall` | "只要天猫""天猫店"→ `true`；"只要淘宝""C店"→ `false` | `None` |
| `sku_keywords` | "16+512""16G 512G"→ 空格分隔关键词 | `None` |
| `need_screenshot` | "截图""证据"→ `true` | `true` |
| `manual_approval_required` | "自动""无人值守"→ `false` | `true` |
| `headless` | "无头""后台静默"→ `true` | `false` |
| `session_strategy` | 通常不需要改，使用默认值 | `"storage_state"` |
| `report_channel` | 结果回传通道，目前支持 `feishu` | `"feishu"` |

**提取示例：**

- "帮我找便宜的蓝牙耳机，100以内包邮" → `search_keyword="蓝牙耳机"`, `price_max=100`, `require_free_shipping=True`
- "搜苹果手机好评率95%以上，前10个" → `search_keyword="苹果手机"`, `rating_threshold=0.95`, `max_candidates=10`
- "天猫上找索尼耳机，付款人数超1000，16G 512G规格" → `search_keyword="索尼耳机"`, `require_tmall=True`, `min_sales=1000`, `sku_keywords="16G 512G"`

---

## 2. 执行协议

### Step 1: 构造命令

根据提取的参数构造 CLI 调用：

```bash
python scripts/taobao.py search \
  --keyword "<关键词>" \
  --rating-threshold <阈值> \
  --max-candidates <数量> \
  --price-min <最低价> --price-max <最高价> \
  --min-sales <最低销量> \
  --require-free-shipping --require-tmall yes \
  --sku-keywords "<规格关键词>"
```

可选 flags：`--no-screenshot`、`--no-manual-approval`、`--headless`、`--no-session-auto-save`。

### Step 2: 执行并读取结果

执行命令，捕获 stdout 作为 JSON 解析。stderr 是浏览器运行日志，忽略。

taobao.py 返回两种 JSON：

**正常完成（success / partial_success / failed）：**
```json
{
  "status": "success|partial_success|failed",
  "task_id": "...",
  "session": {"status": "restored|missing|captured", "logged_in": true|false},
  "search": {"status": "...", "keyword": "...", "candidates_found": N},
  "items": [
    {
      "title": "商品标题",
      "item_id": "...",
      "price": "¥299.00",
      "price_value": 299.0,
      "sales_count": 15000,
      "rating": 0.98,
      "free_shipping": true,
      "is_tmall": true,
      "url": "https://...",
      "cart_added": true
    }
  ],
  "skipped": [
    {"title": "...", "reason": "sku_or_price_mismatch", "detail": "..."}
  ],
  "evidence": ["screenshot_paths"],
  "steps": [{"name": "...", "status": "...", "message": "..."}],
  "error": null,
  "resume_stage": null
}
```

**中断信号（need_login / need_captcha）：**
```json
{
  "status": "need_login|need_captcha",
  "task_id": "...",
  "session": {"status": "...", "logged_in": false},
  "message": "人类可读的中断原因",
  "action": "告知用户如何操作",
  "resume_stage": "awaiting_login|awaiting_captcha",
  "steps": [...]
}
```

中断信号下没有 `items`/`search`/`skipped` 字段，因为搜索尚未完成。执行 `resume` 后会重新走完整流程。

### Step 3: 根据 status 决定下一步

| status | 含义 | 你应该做的事 |
|--------|------|-------------|
| `success` | 全部完成，商品已加购 | 向用户汇报 `items` 列表，展示关键信息 |
| `partial_success` | 部分完成（无匹配商品） | 说明 `skipped` 原因，建议放宽条件 |
| `need_login` | 需要手动登录 | 第 3 节中断处理流程 |
| `need_captcha` | 需要手动过验证码 | 第 3 节中断处理流程 |
| `failed` | 执行异常 | 查看 `error.code`，决定重试/放弃/求助用户 |

---

## 3. 中断处理：登录与验证码

### 收到 `need_login` 时

taobao.py 的 session 未登录。你必须：

1. **告知用户：** "淘宝未登录，请在弹出的浏览器窗口中手动完成登录（扫码或账号密码），完成后告诉我。"
2. **等待用户确认**（用户说"好了""完成""done"）
3. **执行恢复：** `python scripts/taobao.py resume`

不要自动重试 `search`——`resume` 会复用已保存的会话状态继续搜索。

### 收到 `need_captcha` 时

搜索被风控拦截。你必须：

1. **告知用户：** "淘宝触发了安全验证，请在浏览器中完成滑块验证，完成后告诉我。"
2. **等待用户确认**
3. **执行恢复：** `python scripts/taobao.py resume`

### 处理超时

如果 `resume` 仍然返回 `need_login` 或 `need_captcha`（最多尝试 2 次）：
- 告知用户："登录/验证似乎未成功，请确认是否已完成操作。也可以尝试清除会话重新开始：`python scripts/taobao.py clear-session`"
- 让用户决定是否继续

---

## 4. 结果解读与汇报

### 汇报格式

向用户自然语言汇报，不要直接 dump JSON。格式参考：

```
搜索「{keyword}」完成，共检查 {N} 个候选商品，成功加购 {M} 件：

✅ 符合条件（好评率 ≥ {threshold}%）：
1. [天猫/淘宝] 商品标题 — ¥价格 — 好评率 X% — 月销 N — 包邮/不包邮
2. ...

⚠️ 好评率不达标：
1. 商品A — 好评率 X%（低于 {threshold}% 阈值）

❓ 好评率未知：
1. 商品B — 详情页未展示好评率

❌ 加购失败：
- 商品C：SKU规格不匹配
```

如果用户未设定好评率阈值，则不需要分"符合/不达标"，直接列出所有加购商品并标注好评率即可。

### 筛选决策

taobao.py 做了基础筛选（价格/销量/包邮/天猫/关键词匹配），并提取了每个商品的**好评率**（`rating` 字段）。

**你需要做的判断（好评率筛选）：**
- 读取每个 item 的 `rating` 字段
- 如果用户设定了好评率阈值：`rating` >= 阈值 → 向用户汇报为"符合"；`rating` < 阈值 → 汇报为"好评率不达标"，但仍告知用户该商品存在
- 如果 `rating` 为 null：说明"该商品详情页未展示好评率，无法判断"
- 不加购的商品（sku/价格不匹配）在 `skipped` 中，分析 reason 向用户解释

**主动建议：**
- 如果符合好评率要求的商品太少：建议放宽阈值或扩大搜索范围
- 如果所有商品好评率都未知：建议用户手动确认

### 证据引用

如果 `evidence` 数组非空，在汇报末尾附上截图路径："搜索和购物车截图已保存，可查看证据。"

---

## 5. 异常处理决策树

### WORKFLOW_ERROR

```
收到 WORKFLOW_ERROR
  ├─ 网络相关错误 → 等待 3 秒，重试 1 次
  ├─ 浏览器启动失败 → 告知用户检查 Playwright 安装
  └─ 其他错误 → 告知用户错误信息，请求指示
```

### 会话过期

如果 `session.status == "missing"` 且 `status == "need_login"`：
- 这是正常的首次使用或会话过期
- 按 3 节流程处理即可

---

## 6. 常用操作

### 检查会话状态

```bash
python scripts/taobao.py check-session
```

返回会话文件是否存在及 cookie 数量。在新任务开始前可以快速检查，避免无效的 `search` 调用。

### 清除会话（重新登录）

```bash
python scripts/taobao.py clear-session
```

当用户反馈"登录失败""会话失效"时使用。下次 `search` 会触发新的手动登录流程。

---

## 依赖与启动

- Python 3.11+
- 安装依赖：`python -m pip install -r requirements.txt`
- 安装浏览器：`python -m playwright install chromium`
- 默认会话路径：`.cache/taobao-search-skill/taobao-session.json`
- 默认截图路径：`.cache/taobao-search-skill/artifacts/`

---

## 失败码参考

| 错误码 | 说明 | 你的处理 |
|--------|------|----------|
| `LOGIN_REQUIRED` | 未登录，需手动完成登录并保存会话 | 中断处理流程 |
| `SEARCH_BLOCKED` | 淘宝风控拦截搜索，需手动完成验证 | 中断处理流程 |
| `WORKFLOW_ERROR` | 工作流执行异常 | 异常处理决策树 |
| `NO_STATE` | resume 时没有可恢复的状态 | 引导用户先执行 search |

---

## 多平台部署

### Claude Code

将本文件复制到 `.claude/skills/taobao-search.md`，Claude Code 自动识别。触发方式：
- 显式：`/taobao-search 搜索苹果手机 好评率大于95%`
- 语义匹配：描述淘宝搜索/加购意图时自动匹配

在 `.claude/settings.local.json` 中确保允许执行：
```json
{
  "permissions": {
    "allow": [
      "python scripts/taobao.py search *",
      "python scripts/taobao.py resume",
      "python scripts/taobao.py check-session",
      "python scripts/taobao.py clear-session"
    ]
  }
}
```

### 直接 CLI

```bash
python scripts/taobao.py search --keyword "苹果手机" --price-max 10000
python scripts/taobao.py search --keyword "耳机" --rating-threshold 0.95 --price-min 50 --price-max 500 --min-sales 1000 --require-free-shipping --require-tmall yes
```
