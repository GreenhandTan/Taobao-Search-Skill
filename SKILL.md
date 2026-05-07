---
name: Taobao-Search-Skill
description: "Search Taobao/Tmall, visually inspect products, select SKUs, add to cart. AI is the brain: sees screenshots, reads DOM, makes decisions in real-time. Script is pure hands: executes atomic browser operations on command."
version: "3.0.0"
allowed-tools: [Bash, Read]
model: "多模态（默认假设支持视觉）"
---

# Taobao-Search-Skill v3

> **合规声明：** 本 Skill 仅用于自动化你自己的淘宝/天猫账号操作。不得绕过平台安全控制、不得用于批量注册/刷单/爬虫等违反淘宝服务条款的行为。使用者对自身操作承担全部责任。

## 核心架构：AI 作为视觉大脑

```
用户需求 → 你(理解意图)
              ↓
         taobao.py search --visual  → 截图 + 商品列表
              ↓
         你 Read 截图 → 视觉分析 → 决定"打开第3个看看"
              ↓
         taobao.py open --index 2  → 详情页截图 + SKU结构
              ↓
         你 Read 截图 → 看到SKU选项 → 判断"选M4芯片+16G内存+512G硬盘"
              ↓
         taobao.py sku-select --label "存储" --value "512G"
              ↓
         你 Read 截图 → 确认选中+价格正确 → 加购
              ↓
         taobao.py cart-add → cart-view → 汇报用户
```

**你（AI Agent）是决策者：** 看截图、读 DOM、判断对错、选择策略、处理异常。
**taobao.py 是执行手：** 打开浏览器、点击元素、输入文字、截图——纯操作，不做判断。

---

## 模式选择

你有两种运作模式：

### 传统模式（`search` 不带 `--visual`）
一次性流水线：搜索→筛选→加购→返回 JSON。适合简单明确的需求。脚本内硬编码选择器做匹配——快但不够灵活。

### 视觉模式（`search --visual` + 后续子命令）【推荐默认】
逐步交互：每步截图返回给你，你看了再决定下一步。**这是本 Skill 推荐的主要模式**，尤其适合：
- SKU 规格复杂（多级选项：颜色→芯片→内存→硬盘）
- 需要视觉确认（"确定是黑色的""确认不是官换机"）
- 价格随 SKU 变化（不同配置不同价）
- 页面布局异常（弹窗、活动层、改版）

---

## 一、感知层：双重输入（截图 + DOM）

你不能只用一种信息来源。截图告诉你"页面长什么样"，DOM 告诉你"可以操作什么"。两者互补。

### 1.1 截图（视觉感知）

每个子命令返回的 JSON 中，`screenshot` 字段是 PNG 文件路径。**每次收到截图后必须用 Read 工具查看**，关注：

| 页面类型 | 重点关注 |
|---------|---------|
| 搜索结果页 | 商品卡片（图+标题+价格）、天猫标识、包邮标签、排序栏、筛选条件 |
| 商品详情页 | SKU 选择区（多组选项：颜色/尺寸/版本）、实时价格、好评率、主图 |
| SKU 选择后 | 选中项是否高亮、价格是否更新、是否有库存提示 |
| 加购后 | 是否弹出"已成功加入购物车"确认浮层 |
| 购物车页 | 商品列表、勾选状态、数量、总价 |

**视觉风险识别：** 看截图时主动识别以下危险信号：
- 标题含 "官换""翻新""后封""99新""二手" → 警告用户
- 大量活动弹窗/悬浮层遮挡 → 先用 `decide` 点击关闭
- 验证码/滑块 → 告知用户手动处理
- 页面空白/加载失败 → 检查 URL 或重试

### 1.2 DOM（结构化感知）

`dom` 子命令提取可见区域的 DOM 元素，标注语义角色（button/link/text_input/sku_option）。**当截图看不清或不确认时，用 `dom` 补充：**

```bash
python scripts/taobao.py dom --task-id <ID>
```

返回的 `data.dom.elements` 包含每个元素的 `role`、`text`、`box`（坐标尺寸），以及 SKU 选项的 `disabled`/`selected` 状态。

**双重感知的典型用法：**
1. 截图看到 SKU 区域 → `dom` 确认有哪些可选值和禁用值
2. 截图看到按钮但不确定是否可点击 → `dom` 看元素状态
3. 截图看到价格但小数位模糊 → 用 `data` 中的 `price_value` 精确字段

---

## 二、行动层：原子操作命令集

所有命令都独立打开浏览器、恢复会话、执行单一操作、截图、关闭。状态通过 `--task-id` 在命令间传递。

### 2.1 核心命令速查

| 命令 | 作用 | 关键参数 |
|------|------|---------|
| `search --visual` | 搜索 + 截图搜索结果 | `--keyword`, `--max-candidates N`, `--price-min/max`, `--require-tmall` |
| `open` | 打开第N个搜索结果，截图详情页 | `--task-id`, `--index N` |
| `sku-select` | 选择一个 SKU 选项 | `--task-id`, `--label "颜色"`, `--value "黑色"` |
| `cart-add` | 点击加入购物车 | `--task-id` |
| `cart-view` | 打开购物车页截图 | `--task-id` |
| `dom` | 提取可见 DOM 结构 | `--task-id`, `--url`（可选） |
| `wait` | 等待条件满足 | `--task-id`, `--condition "selector:."` 或 `"text:"` 或 `"networkidle"` |
| `decide` | **逃生舱**：通用操作 | `--task-id`, `--action click\|scroll\|hover\|press\|type\|navigate`, `--value "..."` |

### 2.2 `decide` 详解（处理意外情况）

当页面出现预期外的弹窗、广告、验证，或你需要非标准操作时：

```bash
# 点击任意可见文字
python scripts/taobao.py decide --task-id X --action click --value "关闭"

# 滚动页面（看更多内容）
python scripts/taobao.py decide --task-id X --action scroll --value "down:800"

# 悬停在元素上（触发下拉菜单）
python scripts/taobao.py decide --task-id X --action hover --value "颜色"

# 键盘按键
python scripts/taobao.py decide --task-id X --action press --value "Escape"

# 直接跳转 URL
python scripts/taobao.py decide --task-id X --action navigate --value "https://..."

# 仅截图（不操作）
python scripts/taobao.py decide --task-id X --action screenshot
```

### 2.3 操作前的登录检查

每次开始新任务前，建议先检查会话状态：

```bash
python scripts/taobao.py check-session
```

如果 `session_exists: false` 或 `cookie_count: 0`，告知用户："首次使用需要手动登录淘宝，过程中会弹出浏览器窗口。登录完成后告诉我继续。"

---

## 三、决策层：你的自主推理框架

### 3.1 任务分解

收到用户需求后，先在心里拆解为步骤序列，不要一次性全交给脚本。例如：

> 用户："帮我找6000以下M4芯片MacBook Air，16+512，13或15寸"

拆解为：
1. 搜索 "MacBook Air M4 16G 512G" → 看搜索结果
2. 逐个打开看起来合适的商品 → 看详情页
3. 每个商品：选芯片(M4) → 选内存(16G) → 选硬盘(512G) → 看价格是否 < 6000
4. 符合条件的 → 加购 → 看购物车确认
5. 汇报用户

### 3.2 动态规划

计划不是死的。遇到以下情况即时调整：

- 搜索结果太少 → 调整关键词（去掉 "M4" 试 "MacBook Air"）
- 搜索结果太多 → 加 `--max-candidates 10` 或 `--require-tmall yes`
- 打开商品后发现 SKU 选项不匹配 → 用 `dom` 看实际选项，调整选择策略
- SKU 有多级依赖 → 一步一步选（先选颜色→截图→再选存储→截图）

### 3.3 异常恢复

| 情况 | 处理 |
|------|------|
| `status: need_login` | 告知用户手动登录，完成后执行原命令 |
| `status: need_captcha` | 告知用户完成验证，完成后重试 |
| `status: failed` + 网络错误 | 等待 3 秒，重试一次 |
| `status: failed` + `NO_ADD_TO_CART_BUTTON` | 用 `decide --action scroll` 滚动页面再试 |
| SKU 选项找不到 | 用 `dom` 查看实际 DOM，调整 `--label` `--value` 关键词 |
| 页面弹窗遮挡 | `decide --action click --value "关闭"` 或 `decide --action press --value "Escape"` |

### 3.4 风险识别清单

在汇报用户之前，必须检查以下风险点：

- [ ] 标题是否含 "官换""翻新""后封""二手""99新""展示机"
- [ ] 价格是否显著低于市场价（>30%）
- [ ] 店铺是否为天猫旗舰店/授权店
- [ ] 好评率是否过低（< 90%）
- [ ] SKU 配置是否与用户需求完全匹配

发现风险时，主动向用户标注并建议跳过。

---

## 四、SKU 选择策略（淘宝核心难点）

淘宝 SKU 通常是多级依赖的：选颜色→选项更新→选芯片→选项更新→选内存→...选项更新→价格变化。

### 策略 A：逐个选择（推荐）

```bash
# Step 1: 打开商品详情
python scripts/taobao.py open --task-id X --index 2
# → 看截图，看到 SKU 组：颜色、芯片、内存、硬盘

# Step 2: 选芯片（最关键的规格）
python scripts/taobao.py sku-select --task-id X --label "芯片" --value "M4"
# → 看截图，确认选中且价格开始显示

# Step 3: 选内存
python scripts/taobao.py sku-select --task-id X --label "内存" --value "16G"
# → 看截图，确认

# Step 4: 选硬盘
python scripts/taobao.py sku-select --task-id X --label "硬盘" --value "512G"
# → 看截图，确认最终价格是否在预算内
```

### 策略 B：先看 DOM 再决定

```bash
# 如果截图看不清 SKU 有哪些选项
python scripts/taobao.py dom --task-id X
# → data.dom.elements 中 role=sku_option 的项列出所有可选值

# 然后精准选择
python scripts/taobao.py sku-select --task-id X --label "版本" --value "M4芯片"
```

### SKU 选择技巧

- `--label` 用 SKU 组的标题关键词（"颜色""存储""版本""芯片""内存""硬盘""尺寸"）
- `--value` 用选项文字的关键部分（"512G" "黑色" "M4" "16G"）
- 选择后立即截图看价格变化——因为不同 SKU 价格不同
- 如果一次 `sku-select` 没选中，换成更短的关键词重试

---

## 五、完整工作流示例

### 示例：搜索 MacBook Air M4

```
用户："帮我找6000以下MacBook Air M4 16+512"

你的完整操作序列：

1. check-session（可选，确认会话正常）
2. search --keyword "MacBook Air M4" --visual --max-candidates 10 --price-max 6000
   → Read 截图：看到 10 个搜索结果...

3. "第1个和第3个看起来靠谱，先看第1个"
   open --task-id X --index 0
   → Read 截图：详情页，看到 SKU 区

4. sku-select --task-id X --label "芯片" --value "M4"
   → Read 截图：M4 选中，价格 ¥5499

5. sku-select --task-id X --label "内存" --value "16G"
   → Read 截图：16G 选中，价格变 ¥5999

6. "5999在预算内，加购"
   cart-add --task-id X
   → Read 截图：看到"已成功加入购物车"

7. "再看第3个商品"
   open --task-id X --index 2
   → Read 截图：看到标题含"官换机"，警告用户并跳过

8. cart-view --task-id X
   → Read 截图：确认购物车，汇报用户
```

### 示例：处理异常弹窗

```
1. search --keyword "蓝牙耳机" --visual
   → Read 截图：首页有618活动弹窗遮挡

2. decide --task-id X --action click --value "关闭"
   → Read 截图：弹窗已关闭，可以继续搜索

   # 如果 click "关闭" 没关掉，试试：
   decide --task-id X --action press --value "Escape"
   → 或 decide --task-id X --action click --value "我知道了"
```

---

## 六、中断处理：登录与验证码

### 收到 `need_login` 时

1. 告知用户："淘宝未登录，请在弹出的浏览器窗口中手动完成登录，完成后告诉我。"
2. 等待用户确认
3. 重新执行刚才失败的命令（会话已保存，会复用）

### 收到 `need_captcha` 时

1. 告知用户："淘宝触发了安全验证，请在浏览器中完成滑块验证，完成后告诉我。"
2. 等待用户确认
3. 重新执行刚才失败的命令

### 清除会话重新开始

```bash
python scripts/taobao.py clear-session
```

---

## 七、结果汇报

完成全部操作后，用自然语言向用户汇报。格式参考：

```
搜索「MacBook Air M4」完成，找到 {N} 件候选商品：

✅ 已加购：
1. [天猫] MacBook Air M4 16G+512G 13寸 — ¥5,999.00 — 好评率 98% — 包邮

⚠️ 已跳过：
- 商品A：标题含"官换机"风险词，已跳过
- 商品B：SKU 选择后价格 ¥6,299 超出预算

截图已保存，如需人工确认可查看。
```

---

## 八、常用维护命令

```bash
# 检查会话
python scripts/taobao.py check-session

# 清除会话
python scripts/taobao.py clear-session

# 传统模式（快速，不交互）
python scripts/taobao.py search --keyword "耳机" --price-max 500 --require-free-shipping

# 视觉模式（默认推荐）
python scripts/taobao.py search --keyword "耳机" --visual --max-candidates 10
```

---

## 九、依赖与环境

- Python 3.11+
- 安装依赖：`python -m pip install -r requirements.txt`
- 安装浏览器：`python -m playwright install chromium`
- 默认会话路径：`.cache/taobao-search-skill/taobao-session.json`
- 视觉状态目录：`.cache/taobao-search-skill/visual-states/`
- 截图目录：`.cache/taobao-search-skill/artifacts/`

---

## 十、多平台部署

### Claude Code

将本文件复制到 `.claude/skills/taobao-search.md`。

`.claude/settings.local.json` 权限配置：
```json
{
  "permissions": {
    "allow": [
      "python scripts/taobao.py search *",
      "python scripts/taobao.py open *",
      "python scripts/taobao.py sku-select *",
      "python scripts/taobao.py cart-add *",
      "python scripts/taobao.py cart-view *",
      "python scripts/taobao.py dom *",
      "python scripts/taobao.py wait *",
      "python scripts/taobao.py decide *",
      "python scripts/taobao.py resume",
      "python scripts/taobao.py check-session",
      "python scripts/taobao.py clear-session"
    ]
  }
}
```

### 其他平台（Cursor、Copilot、OpenClaw）

复制本文件到对应 Skill/Rules 目录，核心要求：执行 Shell 命令 + Read 截图 + 解析 JSON。

---

## 失败码参考

| 错误码 | 说明 | 你的处理 |
|--------|------|----------|
| `LOGIN_REQUIRED` | 未登录 | 告知用户手动登录 |
| `SEARCH_BLOCKED` | 风控拦截 | 告知用户手动验证 |
| `NO_VISUAL_STATE` | 任务状态丢失 | 重新 `search --visual` |
| `INVALID_INDEX` | 商品索引越界 | 检查 `data.items` 长度 |
| `NO_ADD_TO_CART_BUTTON` | 找不到加购按钮 | 用 `decide` 滚动或截图排查 |
| `WORKFLOW_ERROR` | 执行异常 | 根据错误信息决定重试/放弃 |
| `NO_STATE` | resume 无可恢复状态 | 先执行 search |
