---
name: Taobao-Search-Skill
description: "Search Taobao/Tmall, inspect products, select SKUs, add to cart. Persistent browser session with stdin/stdout JSON protocol. AI decides, script executes."
version: "4.0.0"
allowed-tools: [Bash, Read]
model: "多模态（默认假设支持视觉）"
---

# Taobao-Search-Skill v4

> **合规声明：** 本 Skill 仅用于自动化你自己的淘宝/天猫账号操作。不得绕过平台安全控制、不得用于批量注册/刷单/爬虫等违反淘宝服务条款的行为。使用者对自身操作承担全部责任。

## 核心架构：Session 模式

浏览器**只启动一次**，通过 stdin/stdout JSON 协议接收命令。命令间共享同一浏览器实例，无需反复启动。

```
用户需求 → 你(理解意图)
              ↓
         Bash: 启动 session 进程（浏览器打开一次）
              ↓
         Bash: stdin 发 {"cmd":"search","keyword":"MacBook Air M4"}
              ← stdout 收 {"data":{"items":[...8个商品...]}}
              ↓
         你看 JSON 数据 → 决定"打开第3个"
              ↓
         Bash: stdin 发 {"cmd":"open","index":2}
              ← stdout 收 {"data":{"sku_groups":[{"label":"芯片","values":[...]},...]}}
              ↓
         你看 JSON 数据 → 决定"选M4+16G+512G"
              ↓
         Bash: stdin 发 {"cmd":"select-sku","selections":[{"label":"芯片","value":"M4"},{"label":"内存","value":"16G"},{"label":"硬盘","value":"512G"}]}
              ← stdout 收 {"data":{"final_price":5999,"all_selected":true}}
              ↓
         5999 < 6000 → 加购
              ↓
         Bash: stdin 发 {"cmd":"cart-add"}
              ← stdout 收 {"data":{"cart_added":true,"confirmed":true}}
              ↓
         汇报用户
```

**你（AI Agent）是决策者：** 解析用户意图、分析搜索结果、选择商品和 SKU、确认价格、判断风险。
**session 进程是执行者：** 操控浏览器、提取数据、返回结构化 JSON。不做决策。

---

## 一、启动会话

```bash
python scripts/taobao.py session --task-id <自定义ID>
```

首次启动会自动打开浏览器并检查登录状态。如果未登录，会等待用户手动完成登录（弹出浏览器窗口）。

启动后，进程进入等待状态，从 stdin 逐行读取 JSON 命令，每条命令处理完后将 JSON 结果写到 stdout。

**退出方式：** 发送 `{"cmd": "quit"}` 或关闭 stdin（进程自动退出，浏览器关闭）。

---

## 二、命令参考

### 2.1 search — 搜索商品

```json
{"cmd": "search", "keyword": "MacBook Air M4 16G 512G", "price_max": 6000, "require_tmall": true}
```

| 参数 | 类型 | 说明 |
|------|------|------|
| keyword | string | **必填**，搜索关键词 |
| price_min | float | 最低价格 |
| price_max | float | 最高价格 |
| min_sales | int | 最低销量 |
| require_free_shipping | bool | 仅包邮 |
| require_tmall | bool | true=仅天猫，false=仅淘宝 |
| max_candidates | int | 最多返回商品数，默认 20 |

**返回 data：**

```json
{
  "keyword": "MacBook Air M4 16G 512G",
  "items": [
    {
      "index": 0,
      "title": "Apple MacBook Air M4 16G+512G 13寸...",
      "url": "https://item.taobao.com/...",
      "price": "¥5499.00",
      "price_value": 5499.0,
      "sales_count": 2300,
      "rating": 0.98,
      "is_tmall": true,
      "free_shipping": true
    }
  ],
  "items_count": 8
}
```

### 2.2 open — 打开商品详情

```json
{"cmd": "open", "index": 2}
```

| 参数 | 类型 | 说明 |
|------|------|------|
| index | int | **必填**，商品在搜索结果中的索引（从 0 开始） |

**返回 data：**

```json
{
  "item": {"title": "...", "price": 5499.0, "sales_count": 2300, "rating": 0.98, ...},
  "sku_groups": [
    {
      "label": "芯片",
      "values": [
        {"text": "M4", "disabled": false, "selected": false},
        {"text": "M3", "disabled": false, "selected": false}
      ]
    },
    {
      "label": "内存",
      "values": [
        {"text": "16G", "disabled": false, "selected": false},
        {"text": "24G", "disabled": false, "selected": false}
      ]
    }
  ],
  "detail_price": 5499.0
}
```

### 2.3 select-sku — 选择 SKU 规格

```json
{"cmd": "select-sku", "selections": [
  {"label": "芯片", "value": "M4"},
  {"label": "内存", "value": "16G"},
  {"label": "硬盘", "value": "512G"}
]}
```

也支持单选：

```json
{"cmd": "select-sku", "selections": {"label": "芯片", "value": "M4"}}
```

| 参数 | 类型 | 说明 |
|------|------|------|
| selections | object 或 array | **必填**，每个选择包含 label（SKU 组名）和 value（选项值） |

**返回 data：**

```json
{
  "all_selected": true,
  "selections": [
    {"label": "芯片", "value": "M4", "ok": true},
    {"label": "内存", "value": "16G", "ok": true},
    {"label": "硬盘", "value": "512G", "ok": true}
  ],
  "final_price": 5999.0,
  "sku_groups": [...更新后的SKU结构...]
}
```

`all_selected: false` 表示部分选择失败，检查 `selections` 中 `ok: false` 的项。

### 2.4 cart-add — 加入购物车

```json
{"cmd": "cart-add"}
```

**返回 data：**

```json
{
  "cart_added": true,
  "confirmed": true,
  "item_index": 2
}
```

`confirmed` 表示是否看到了"已加入购物车"的确认弹窗。

### 2.5 cart-view — 查看购物车

```json
{"cmd": "cart-view"}
```

**返回 data：**

```json
{
  "cart_item_count": 3,
  "items": [...本次会话中处理过的商品列表...]
}
```

### 2.6 dom — 提取页面 DOM 结构

```json
{"cmd": "dom", "url": "https://..."}
```

`url` 可选，不提供则使用当前页面。返回可见区域的按钮、链接、输入框、SKU 选项等元素，包含坐标和语义角色。

### 2.7 screenshot — 截取当前页面

```json
{"cmd": "screenshot"}
```

返回当前视口截图路径和页面文本摘要。

### 2.8 quit — 退出会话

```json
{"cmd": "quit"}
```

关闭浏览器，退出进程。

---

## 三、响应格式

所有命令返回统一 JSON 结构：

```json
{
  "status": "success",         // "success" 或 "error"
  "task_id": "taobao-abc123",
  "screenshot": ".cache/.../xxx.png",   // 截图文件路径（可用 Read 工具查看）
  "page_text_summary": "...",           // 页面文本前 500 字符
  "data": { ... }                       // 命令特定的返回数据
}
```

错误时：

```json
{
  "status": "error",
  "task_id": "taobao-abc123",
  "error": {"message": "No search results. Run 'search' first."}
}
```

---

## 四、决策框架

### 4.1 数据驱动优先

结构化 JSON 数据是你决策的主要依据，**大多数情况下不需要看截图**：

| 决策 | 数据来源 | 需要截图？ |
|------|---------|-----------|
| 选择哪个商品 | `items[].title/price/rating/is_tmall` | ❌ |
| SKU 是否可选 | `sku_groups[].values[].disabled` | ❌ |
| 价格是否在预算 | `final_price` | ❌ |
| 加购是否成功 | `cart_added / confirmed` | ❌ |
| 标题是否含风险词 | `item.title`（正则匹配） | ❌ |

### 4.2 需要看截图的场景

| 场景 | 原因 |
|------|------|
| 弹窗/活动遮挡 | JSON 无法反映页面视觉遮挡 |
| 验证码/滑块 | 需要视觉确认验证码类型 |
| 加购后确认 | 确认弹窗可能被遗漏 |
| 异常页面 | 页面空白/加载失败/布局异常 |
| 调试失败原因 | 元素找不到时排查页面状态 |

看截图时使用 Read 工具查看 `screenshot` 字段返回的 PNG 路径。

### 4.3 风险识别

在汇报用户前，用正则检查以下风险信号（基于 JSON 数据，不需要截图）：

```
标题含 "官换""翻新""后封""99新""二手""展示机" → 警告用户
价格显著低于市场价（>30%） → 警告用户
is_tmall=false 且用户要求正品 → 提示风险
好评率 < 90% → 提示风险
```

---

## 五、SKU 选择策略

淘宝 SKU 通常是多级依赖的：选颜色→选项更新→选芯片→选项更新→选内存→价格变化。

### 策略 A：批量选择（推荐）

一次性发送所有选择，脚本内部依次执行：

```json
{"cmd": "select-sku", "selections": [
  {"label": "芯片", "value": "M4"},
  {"label": "内存", "value": "16G"},
  {"label": "硬盘", "value": "512G"}
]}
```

适用于：你对 SKU 结构已经清楚（从 `open` 返回的 `sku_groups` 中获取）。

### 策略 B：逐个选择 + 确认

先选最关键的规格，看价格变化后再决定下一步：

```
1. select-sku {"label":"芯片","value":"M4"}  → 看 final_price
2. 如果价格合理 → select-sku {"label":"内存","value":"16G"} → 看 final_price
3. 继续...
```

适用于：价格随 SKU 变化大、需要逐步确认预算。

### 选择技巧

- `label` 用 SKU 组的标题关键词（"颜色""存储""版本""芯片""内存""硬盘""尺寸"）
- `value` 用选项文字的关键部分（"512G" "黑色" "M4" "16G"）
- 如果 `ok: false`，尝试用更短的关键词重试
- `sku_groups` 中 `disabled: true` 的选项不可选（缺货）

---

## 六、完整工作流示例

### 示例：搜索 MacBook Air M4

```
用户："帮我找6000以下MacBook Air M4 16+512"

你的操作序列：

1. 启动会话
   python scripts/taobao.py session --task-id macbook-task

2. 搜索
   → {"cmd":"search","keyword":"MacBook Air M4 16G 512G","price_max":6000}
   ← items: 8个候选商品
   判断：第0个(¥5499,天猫,好评98%)和第2个(¥5899,天猫,好评96%)看起来靠谱

3. 打开第0个
   → {"cmd":"open","index":0}
   ← sku_groups: 芯片(M4/M3), 内存(16G/24G), 硬盘(256G/512G/1T)
   判断：有M4+16G+512G可选

4. 选择规格
   → {"cmd":"select-sku","selections":[{"label":"芯片","value":"M4"},{"label":"内存","value":"16G"},{"label":"硬盘","value":"512G"}]}
   ← final_price: 5999, all_selected: true
   判断：5999 < 6000，符合预算

5. 加购
   → {"cmd":"cart-add"}
   ← cart_added: true, confirmed: true

6. 查看购物车
   → {"cmd":"cart-view"}
   ← cart_item_count: 1

7. 退出
   → {"cmd":"quit"}

汇报用户：
搜索「MacBook Air M4 16G 512G」完成：
✅ 已加购：MacBook Air M4 16G+512G 13寸 — ¥5,999.00 — 好评率 98% — 天猫 — 包邮
请前往购物车确认。
```

### 示例：处理异常

```
→ {"cmd":"open","index":3}
← status: error, "Item has no URL"
判断：跳过这个商品，试下一个

→ {"cmd":"open","index":4}
← sku_groups: [{label:"颜色", values:[{text:"黑色",disabled:true}]}]
判断：想要的颜色缺货，跳过

→ {"cmd":"select-sku","selections":{"label":"颜色","value":"白色"}}
← all_selected: false, selections: [{ok: false}]
判断：选择失败，可能是关键词不匹配。截图排查：
→ {"cmd":"screenshot"}
← screenshot: ".cache/.../xxx.png"
Read 截图 → 看到选项文字是"象牙白"不是"白色"
→ {"cmd":"select-sku","selections":{"label":"颜色","value":"象牙白"}}
← all_selected: true, final_price: 3299
```

---

## 七、异常处理

### 登录失效

搜索或其他命令返回页面内容异常（空结果、被重定向到登录页）时：

```bash
# 检查会话状态
python scripts/taobao.py check-session

# 如果 session 进程还在运行，发送 quit 重启
# 重新启动 session，首次启动会自动检测登录
python scripts/taobao.py session --task-id <ID>
```

### 验证码

命令返回的 `screenshot` 中出现滑块/验证码时：

1. 告知用户："淘宝触发了安全验证，请在浏览器中完成验证，完成后告诉我。"
2. 等待用户确认
3. 重新发送刚才的命令

### 元素找不到

`cart-add` 返回 `error: "Add-to-cart button not found"` 时：

1. 发送 `{"cmd":"screenshot"}` 查看页面状态
2. 可能需要滚动页面找到按钮——但当前 session 模式暂不支持滚动操作
3. 尝试重新 `open` 该商品再 `cart-add`

---

## 八、依赖与环境

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

- Python 3.11+
- 默认会话路径：`.cache/taobao-search-skill/taobao-session.json`
- 截图目录：`.cache/taobao-search-skill/artifacts/`
