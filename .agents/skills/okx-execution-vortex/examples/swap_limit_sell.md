# 示例：永续合约限价卖出

## 用户输入
```
对 ETH-USDT-SWAP 限价卖出 10 张，价格 2500
```

## Skill 处理流程

### 1. 解析指令
- 识别操作：`限价卖出`
- 识别标的：`ETH-USDT-SWAP`
- 识别数量：`10 张`
- 识别价格：`2500` USDT
- 识别类型：永续合约限价单

### 2. 查询合约详情
```bash
okx market instruments --instType SWAP --instId ETH-USDT-SWAP
```
返回：
```json
{
  "instId": "ETH-USDT-SWAP",
  "ctVal": "0.1",
  "ctMult": "1",
  "ctValCcy": "ETH",
  "lotSz": "1",
  "minSz": "1",
  "tickSz": "0.01",
  "maxLmtSz": "10000",
  "maxMktSz": "2000"
}
```

### 3. 计算名义价值
```
10 张 × 0.1 ETH/张 × 2500 USDT/ETH = 2,500 USDT 名义价值
```

### 4. 风险检查
```bash
okx account balance --ccy USDT
okx swap positions --instId ETH-USDT-SWAP
```
返回：
```json
{
  "totalEq": "20000.00",
  "available": "18000.00"
}
```
```json
{
  "pos": []
}
```

单笔风险计算：2,500 ÷ 20,000 = 12.5% > 2% (阈值)

**自动调整**：400 USDT 名义价值 (2% of 20,000)

调整后张数：400 ÷ (0.1 × 2500) = 1.6 张 → 向下取整 1 张（因 `lotSz=1`）

### 5. 保证金计算（10x杠杆）
```bash
okx swap leverage --instId ETH-USDT-SWAP --lever 10 --mgnMode isolated
```
保证金 = 名义价值 ÷ 杠杆 = (1张 × 0.1 × 2500) ÷ 10 = 25 USDT

### 6. 二次确认
```
⚠️ **交易确认请求**

市场：ETH-USDT-SWAP
操作：卖出开空
价格：限价 2,500.00 USDT
数量：1 张（名义价值 250 USDT）
杠杆：10x（逐仓）
保证金：25.00 USDT
预估手续费：0.125 USDT (0.05% Maker)
账户权益：20,000.00 USDT
单笔风险：1.25%

📊 **风险检查结果**：
✅ 保证金率：450%（>150% 安全线）
✅ 可用余额充足（18,000 USDT）
✅ 最大可开仓：80 张（当前 1 张）
✅ 系统状态：正常

⚠️ **注意**：
- 当前资金费率：-0.01%（空头收取）
- 标记价格：2,450.50 USDT
- 强平价格：≈2,275.00 USDT（距离 9.0%）

请确认是否执行？（Y/N）
```

### 7. 用户确认
用户输入：`Y`

### 8. 执行命令
```bash
okx swap place \
  --instId ETH-USDT-SWAP \
  --side sell \
  --posSide short \
  --sz 1 \
  --px 2500 \
  --tgtCcy base_ccy \
  --tdMode isolated \
  --lever 10 \
  --ordType limit
```

### 9. 返回结果
```json
{
  "code": "0",
  "msg": "",
  "data": [
    {
      "ordId": "234567890",
      "clOrdId": "web3_xiaoyao_20240115_143530",
      "tag": "",
      "sCode": "0",
      "sMsg": "",
      "px": "2500.00",
      "sz": "1",
      "fee": "0.125000",
      "feeCcy": "USDT",
      "state": "live",
      "ts": "2024-01-15T14:35:30.000Z"
    }
  ]
}
```

### 10. 格式化输出
```
✅ 限价单已挂出！

📋 订单详情：
- 订单ID：234567890
- 市场：ETH-USDT-SWAP
- 方向：卖出开空
- 价格：2,500.00 USDT
- 数量：1 张（0.1 ETH，名义价值 250 USDT）
- 杠杆：10x（逐仓）
- 占用保证金：25.00 USDT
- 手续费：0.125 USDT (0.05% Maker，成交后收取)
- 挂单时间：2024-01-15 14:35:30
- 订单状态：等待成交

💰 保证金变化：
- 可用保证金：-25.00 USDT
- 冻结保证金：+25.00 USDT
- 总保证金占用：25.00 USDT

📊 风险状态：
- 单笔风险：1.25% ✅
- 总仓位风险：1.25% ✅
- 强平价格：≈2,275.00 USDT（距离当前价 9.0%）
- 资金费率影响：-0.01%/8h（空头收取）

🔔 建议设置止盈止损：
- 止盈：2,650.00 USDT（盈利 6%）
- 止损：2,400.00 USDT（亏损 4%）
```

## 关键逻辑说明

1. **合约张数计算**：考虑 `ctVal` (0.1 ETH/张)、`lotSz` (1张最小单位)
2. **名义价值**：张数 × ctVal × 价格
3. **杠杆调整**：使用 `--lever` 参数，自动计算所需保证金
4. **`--tgtCcy base_ccy`**：以标的货币（张数）指定数量
5. **`--posSide short`**：明确为空头仓位
6. **资金费率提醒**：空头需支付资金费时特别提示

## 高级功能：自动设置止盈止损

用户可追加指令：“并设置止盈 2650，止损 2400”

### 执行 OCO 订单
```bash
okx algo place \
  --instId ETH-USDT-SWAP \
  --side buy \
  --posSide short \
  --sz 1 \
  --tpTriggerPx 2650 \
  --slTriggerPx 2400 \
  --tpOrdPx -1 \  # -1 表示市价止盈
  --slOrdPx -1    # -1 表示市价止损
```

## 测试命令（--demo 模式）
```bash
okx swap place --instId ETH-USDT-SWAP --side sell --posSide short --sz 1 --px 2500 --tgtCcy base_ccy --tdMode isolated --lever 10 --ordType limit --demo
```

## 错误处理场景

### 场景1：价格超出限制
```
❌ 价格无效！
当前卖一价：2,450.50 USDT
限价卖单价格：2,500.00 USDT
要求：限价卖单价格需 ≥ 卖一价的 101%（2,475.01 USDT）
建议价格：≥ 2,475.01 USDT
```

### 场景2：杠杆过高
```
❌ 杠杆超过限制！
账户等级：Lv.1
最大杠杆：20x
请求杠杆：50x
建议调整：≤ 20x
```

### 场景3：仓位方向冲突
```
⚠️ 发现反向仓位！
当前持有：ETH-USDT-SWAP 多头 5 张
新开仓位：空头 1 张
结果：净仓位 4 张多头
是否继续？（Y/N）
```