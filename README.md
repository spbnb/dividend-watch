# 红利观察台

[打开监控看板](https://spbnb.github.io/dividend-watch/) · [查看工作流](https://github.com/spbnb/dividend-watch/actions/workflows/monitor.yml)

GitHub Actions 在盘中抓取行情，判断自定义价位、RSI 和温度，并向已配置的渠道发送提醒。GitHub Pages 显示最近一次巡检快照。清单包含 6 条指数、5 只 A 股与 1 只场外基金。

## 开通通知

打开仓库 **Settings → Secrets and variables → Actions → New repository secret**。可以同时启用多个渠道，密钥仅填在仓库 Secrets 中。[打开 Secrets 设置](https://github.com/spbnb/dividend-watch/settings/secrets/actions)

| 渠道 | Secret 名称 | 配置方法 |
| --- | --- | --- |
| 邮件 | `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO` | 填发信服务器、发件邮箱、SMTP 授权码和收件地址。QQ 邮箱可用 `smtp.qq.com`。多个收件地址用英文逗号分隔。 |
| 邮件端口 | `SMTP_PORT`（可选） | 默认 465，使用 SSL。服务商要求 STARTTLS 时填 587。 |
| 微信 | `PUSHPLUS_TOKEN` | 在 PushPlus 用微信关注并绑定账号，填入一对一推送 token。[PushPlus](https://www.pushplus.plus/) |
| ntfy.sh（安卓 / 网页） | `NTFY_TOPIC`，受保护主题可另填 `NTFY_TOKEN` | 在安卓应用或网页订阅同一主题，填入主题名或完整主题地址。公开主题的订阅者均能读到消息。[ntfy 网页版](https://ntfy.sh/app) |
| WxPusher 个人推送 | `WXPUSHER_SPT` | 通过官方页面获取个人 `SPT_` 令牌，填入后即可接收推送。[获取个人 SPT](https://wxpusher.zjiecode.com/docs/spt.html) |
| 飞书 | `FEISHU_WEBHOOK`，开启加签时另填 `FEISHU_SECRET` | 在接收消息的群添加自定义机器人，复制 Webhook 和签名密钥。使用关键词校验时，可设为“红利观察台”。[机器人配置指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) |

在 **Actions → Dividend Watch → Run workflow** 选择 `test`，再选要测试的渠道。选 `all` 时会向每个已配置的渠道发送一条测试消息。默认选项 `refresh` 只更新看板。发送结果记录在运行日志，服务商接受请求与终端实际收到消息可分别核对。

## 巡检时间与规则

交易日北京时间 **08:00** 推送盘前温度榜，显示上一交易日收盘温度。**16:30** 推送盘后温度榜，显示当日已更新的收盘温度，待公布净值等标的单列。两份榜单由 GitHub 发送，各渠道每期各一次。盘前使用深交所交易日历，休市日跳过。盘前 08:30、09:00 的巡检可补发，盘后 17:30 有一次补发机会。若服务商回执不明确，会保留发送记录以避免重复。

周一至周五 **08:00 至 14:30** 每个整点和半点计划巡检。在相应市场的交易时段出现新的阈值信号时推送盘中提醒。14:30 是盘中巡检最后一个排队时刻，若该轮延迟，在 15:00 前仍检查最新有效行情。GitHub 的定时任务可能延迟或被丢弃，两次快照之间短暂越过目标价也可能错过。公开仓库连续 60 天没有活动时，GitHub 会停用定时工作流，可在 Actions 页重新启用。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

- 指数、股票和场外基金的配置在 `watchlist.json`。首页按温度排名，标注数据日期。点名称进入温度页，点「行情」查看报价与观测口径。
- 12 个标的都计算 Wilder 日线 RSI(14) 和 RSI(7)。两档提醒分别为 35/30 和 25/20，等于阈值即命中。股票使用复权价，012708 使用已公布的净值日收益，盘中用估值涨跌幅暂估。指数从自身的日线计算温度、两个 RSI、MACD 和已配置的回撤观察。
- 两个 RSI 周期独立判断，同一端一次巡检命中多个条件时合并推送。中证红利低波动指数 H30269 沿用温度推送，RSI 和 60 日收盘回撤仅在看板观察。其他五条指数按已配置的 RSI 和温度阈值推送。
- 综合温度 = 50% × 价格位置 + 35% × RSI(14) + 15% × RSI(7)。价格位置比较此前 250 个收盘，同值计半；三个分项均齐备后才出分，按一位小数分档。≤10° 为超低温，超过 10° 至 25° 为低温。详情和盘前、盘后榜区分已公布收盘与盘中或收盘涨跌暂估。
- 六条指数直接监测价格指数点位，技术指标和历史收益使用各自明确的日线序列。中证红利低波动 H30269 用 H20269，中证红利 000922 用 H00922，国证自由现金流 980092 用 480092，中证港股通高股息投资 930914 用 H20914，恒生港股通高股息低波动 HSHYLV 用 HSHYLV TRI。上述五条为全收益。标普中国 A 股大盘红利低波 50 使用 SPCLLHCP 价格指数。港股两项按港元计价。
- 温区历史统计用同一条指数日线，从信号后下一次指数收盘起算，满 1、2、3 个日历年后取首个可用收盘。仅统计发布后、现行规则起算的到期样本，同一轮市场中的多日持有期可能重叠。指数分红率另列来源和日期，缺值显示「—」。[指数策略回测](research/strategy-backtest.md) · [股息率与国债利差实验](research/value-results.md)
- 六条指数使用温度和 RSI 阈值。原基金以元/份设定的价格档位随监控对象切换退役。股票和 012708 仍使用各自的两档价格或净值。
- 手机标题标出本次新触发的价格、RSI 或指数条件。WxPusher 按标的分卡，ntfy 按标的分段。卡内的“到首档还需跌”按（现价 − 首档价）÷ 现价计算。行情过期时显示待更新，基金估值与跟踪指数分别标注时间。
- 指数历史缺数时显示最近可用的指数读数及日期。盘中只用通过交易时段和时效核验的同一价格指数行情，按同币种价格指数涨跌暂估当日全收益。在最新股息再投资因素公布前，暂估与最终指数收盘可能有差异。
- 股票和场外基金的近 365 日分红率，按除息日统计现金分红，再除以最新股价或净值。已核验没有分红时显示 0.00%，数据不足显示「—」。
- 场外 012708 的单位净值或盘中估值按 1.36/1.30 元两档判断。其跟踪指数 931446（东证红利低波）按当日涨跌幅 ≤ −1.5% / ≤ −2.5% 独立提醒。盘中估值缺失时，仅用紧邻上一交易日的净值乘以 `1 + 931446 当日涨跌幅` 推算。申购按基金公司确认的净值结算。[中证指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208180546-931446_Index_Methodology_cn.pdf)
- 每个渠道实际发送前单独检查市场是否处于交易时段、基准行情是否更新、信号是否在最近 25 分钟内。港股与内地交易时段分别判断。旧行情、停牌和缺数会标注。两个行情源全部失败且无可用数据时，保留已发布页面。
- 本机和 GitHub 分别记录北京时间当天的标的、指标、档位和渠道，消息标题标明来源。首档后到二档可再发一条。直接到二档同时覆盖该指标的首档。价格、RSI(14)、RSI(7)、指数跌幅各自计算升级。每端发送前先保存记录，云端作为短期 Actions artifact 保存经过 HMAC 处理的标识。本机或云端遇到回执超时会保留占用记录，当天不重复发送同一事件。明确拒绝的渠道可在条件仍有效时重试。

## 本地运行

使用 Python 3.11 及以上版本。中证指数的股息率来自官方 Excel 工作表，由 xlrd 读取。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

本机打开 `http://127.0.0.1:8000`。也可以直接打开生成的 `docs/index.html`。最新机器可读快照在 `docs/snapshot.json`。

配套的本地 Vibe AStock 从同一份 `watchlist.json` 读取规则。打开程序后，交易时段约每 30 秒轮询一次最新行情，交易结束后待命。每次启动会先检查一次行情。提醒页显示最近巡检时间与报价覆盖数，命中条件写入收件箱并推送到已启用的渠道。线上和本机各自记录回执，同一条件可能分别收到两条。

## 部署到另一个 GitHub 仓库

将源码推送到 `main`，在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。更新工作流中的 `DASHBOARD_URL` 及页面和 README 中的仓库链接，然后手动运行一次 `refresh`。工作流构建并发布 Pages artifact，定时执行无需写回源代码。[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
