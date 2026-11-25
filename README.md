# 红利观察台

[打开监控看板](https://spbnb.github.io/dividend-watch/) · [查看工作流](https://github.com/spbnb/dividend-watch/actions/workflows/monitor.yml)

GitHub Actions 在盘中抓取行情、判断自定义价位与指标，并向已配置的渠道发送提醒。GitHub Pages 显示最近一次成功巡检的快照。清单包含 11 组标的、12 条报价，中国海油 A/H 股分别用人民币和港元计价。

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

北京时间周一至周五 **08:00 至 14:30**，每个整点和半点计划巡检，共 14 次。只有在相应市场的交易时段出现新买点才推送。14:30 是最后一个排队时刻，若该轮延迟，在 15:00 前仍检查最新有效行情，15:00 起仅更新看板。GitHub 的定时任务可能延迟或被丢弃，两次快照之间短暂越过目标价也可能错过。公开仓库连续 60 天没有活动时，GitHub 会停用定时工作流，可在 Actions 页重新启用。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

- 价位和指标在 `watchlist.json` 中修改。页面默认按 `(现价 / 首档 − 1) × 100%` 升序排列，点标的名称查看数据时间、两个档位和命中原因。
- 全部 12 条报价都计算日线 RSI(14) 和 RSI(7)，使用 Wilder 平滑。RSI(14) 两档为 35/30，RSI(7) 两档为 25/20，等于阈值即命中。股票和场内 ETF 使用本市场前复权日线，盘中以最新价暂估当日收盘。中国海油 A、H 股独立计算，分别用上证指数和恒生指数行情校验上一交易日。场外 012708 按基金已公布的日净值涨跌幅连乘计算 RSI，盘中以估值涨跌幅补入当天，缺少有效历史时显示缺数。
- 两个 RSI 周期与价格独立判断，同一端一次巡检同时命中时合并成一条。512890 的 RSI、价格和 60 日收盘回撤都仅在看板观察。MACD 柱仅供查看。
- 手机标题标出本次新触发的价格、RSI 或指数条件。WxPusher 按标的分卡，ntfy 按标的分段。卡内的“到首档还需跌”按（现价 − 首档价）÷ 现价计算。行情过期时显示待更新，基金估值与跟踪指数分别标注时间。
- 159545 和 513530 先判断价位或 RSI，再核对 IOPV。溢价高于 1.5% 时在看板标注拦截，IOPV 缺失或港股基准停更时暂停买点提醒。按 `(交易价 / IOPV − 1) × 100%` 计算溢价。
- 中国平安和招商银行的股息率用过去 365 天除息的每股现金股息合计除以现价。股息率观察线仅作对照，历史分红取不到时显示缺数。
- 场外 012708 的单位净值或盘中估值按 1.36/1.30 元两档判断。其跟踪指数 931446（东证红利低波）按当日涨跌幅 ≤ −1.5% / ≤ −2.5% 独立提醒。盘中估值缺失时，仅用紧邻上一交易日的净值乘以 `1 + 931446 当日涨跌幅` 推算。申购按基金公司确认的净值结算。[中证指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208180546-931446_Index_Methodology_cn.pdf)
- 每个渠道实际发送前单独检查市场是否处于交易时段、基准行情是否更新、信号是否在最近 25 分钟内。港股与内地交易时段分别判断。旧行情、停牌和缺数会标注。两个行情源全部失败且无可用数据时，保留已发布页面。
- 本机和 GitHub 分别记录北京时间当天的标的、指标、档位和渠道，消息标题标明来源。首档后到二档可再发一条。直接到二档同时覆盖该指标的首档。价格、RSI(14)、RSI(7)、指数跌幅各自计算升级。每端发送前先保存记录，云端作为短期 Actions artifact 保存经过 HMAC 处理的标识。本机或云端遇到回执超时会保留占用记录，当天不重复发送同一事件。明确拒绝的渠道可在条件仍有效时重试。

## 本地运行

Python 3.11 及以上，仅需标准库。

```sh
python -m unittest discover -s tests -v
python monitor.py
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

本机打开 `http://127.0.0.1:8000`。也可以直接打开生成的 `docs/index.html`。最新机器可读快照在 `docs/snapshot.json`。

配套的本地 Vibe AStock 从同一份 `watchlist.json` 读取规则。打开程序后，交易时段约每 30 秒轮询一次最新行情，交易结束后待命。每次启动会先检查一次行情。提醒页显示最近巡检时间与报价覆盖数，命中条件写入收件箱并推送到已启用的渠道。线上和本机各自记录回执，同一条件可能分别收到两条。

## 部署到另一个 GitHub 仓库

将源码推送到 `main`，在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。更新工作流中的 `DASHBOARD_URL` 及页面和 README 中的仓库链接，然后手动运行一次 `refresh`。工作流构建并发布 Pages artifact，定时执行无需写回源代码。[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
