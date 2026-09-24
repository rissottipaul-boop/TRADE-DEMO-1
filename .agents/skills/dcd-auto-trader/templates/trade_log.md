# DCD 双币赢交易记录

> 合并有效成本：无（尚无 BTC 持仓）
> 累计 USDG/USDT 权利金收入：0
> 累计 BTC 权利金收入：0 BTC

---

## 记录格式

每笔交易按以下格式追加：

```
### YYYY-MM-DD HH:MM — PUT/CALL 下单/结算

- **产品**: productId | 行权价 xxxxx | 到期 x天
- **方向**: PUT 低买 / CALL 高卖
- **投入**: xxxx USDG/USDT 或 x.xxxx BTC
- **APY**: xx.x%
- **安全距离**: x.xx%（安全下界: xxxxx）
- **v3 波动率**: vol_24h=x.xxxx（IV=x.xx, ATR_fast=x.xx, ATR_slow=x.xx, BB=x.xx, 资金费率=x.xx）
- **事件乘数**: x.x（事件类型/无事件）
- **结算结果**:（到期后填写）未行权 ✅ / 被行权 ❌
  - 收到: xxxx USDG 或 x.xxxx BTC
  - 权利金: xxxx USDG 或 x.xxxx BTC

资产快照: USDG=xxxx | USDT=xxxx | BTC=x.xxxx
合并有效成本更新:（如有变化）旧值 → 新值
```

---

（交易记录将在首次下单后自动更新）
