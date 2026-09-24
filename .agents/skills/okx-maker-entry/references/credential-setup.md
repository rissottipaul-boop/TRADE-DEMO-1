# OKX 首次配置引导

当 `doctor` 报 config 文件找不到、profile 缺失、或 `canTrade: false` 时，按以下步骤完成首次配置。

## Step 1：生成配置模板

```bash
python "<skillDir>/scripts/okx_maker_entry.py" init
```

该命令会在 `~/.okx/config.toml` 写入一份空模板（若文件已存在则不会覆盖）。模板包含 `demo`（模拟盘）与 `live`（实盘）两个 profile 段，字段为空。

## Step 2：在 OKX 创建 API Key

引导用户进入 OKX 官方网页或 App 的 API 管理页面：

1. 新建 API Key
2. 权限至少勾选 **Read + Trade**
3. 可按需绑定 IP 白名单
4. 模拟盘与实盘的 Key 不通用 —— 如果要用实盘，需要单独在实盘环境再建一把

## Step 3：填写 `~/.okx/config.toml`

用户需要把生成的字段填到 `~/.okx/config.toml` 对应的 profile 段中。助手可以用 Edit 工具协助用户编辑该文件 —— 按 profile 块分别填入 API Key、Secret、Passphrase 三个字段即可，格式参考 `init` 命令生成的模板本身。

建议先只配置 `demo` profile 做验证，确认流程跑通后再配置 `live`。

## Step 4：doctor 验证

```bash
python "<skillDir>/scripts/okx_maker_entry.py" doctor --profile demo --instId BTC-USDT-SWAP
```

确认输出包含：

- `canRead: true`
- `canTrade: true`
- `oneWayMode: true`
- `symbolTradable: true`

全部通过即完成首次配置，可以回到原始开仓请求继续。

## 常见问题

- **`oneWayMode: false`**：账户处于对冲模式。到 OKX 交易设置 > 持仓模式，切换为"单向持仓"，再重跑 doctor。
- **`canTrade: false`**：API Key 未勾 Trade 权限，重建 Key 时勾上 Read + Trade。
- **`ModuleNotFoundError: No module named 'ccxt'`**：Python 环境没装 ccxt，用错误消息中的 `sys.executable` 路径跑 `"<该路径>" -m pip install ccxt`（务必用 `-m pip`，裸 `pip install` 在多 Python 环境下会装到别的解释器）。
