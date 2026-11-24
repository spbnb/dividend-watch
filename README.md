# 红利观察台

[打开监控看板](https://spbnb.github.io/dividend-watch/) · [查看工作流](https://github.com/spbnb/dividend-watch/actions/workflows/monitor.yml)

GitHub Actions 在盘中抓取行情、判断自定义价位与指标，并向已配置的渠道发送提醒。GitHub Pages 显示最近一次成功巡检的快照。清单包含 11 组标的、12 条报价，中国海油 A/H 股分别用人民币和港元计价。

## 开通通知

打开仓库 **Settings → Secrets and variables → Actions → New repository secret**。可以同时启用邮件、微信和飞书，密钥仅填在仓库 Secrets 中。[打开 Secrets 设置](https://github.com/spbnb/dividend-watch/settings/secrets/actions)

| 渠道 | Secret 名称 | 配置方法 |
| --- | --- | --- |
| 邮件 | `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO` | 填发信服务器、发件邮箱、SMTP 授权码和收件地址。QQ 邮箱可用 `smtp.qq.com`。多个收件地址用英文逗号分隔。 |
| 邮件端口 | `SMTP_PORT`（可选） | 默认 465，使用 SSL。服务商要求 STARTTLS 时填 587。 |
| 微信 | `PUSHPLUS_TOKEN` | 在 PushPlus 用微信关注并绑定账号，填入一对一推送 token。[PushPlus](https://www.pushplus.plus/) |
| 飞书 | `FEISHU_WEBHOOK`，开启加签时另填 `FEISHU_SECRET` | 在接收消息的群添加自定义机器人，复制 Webhook 和签名密钥。使用关键词校验时，可设为“红利观察台”。[机器人配置指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) |

在 **Actions → Dividend Watch → Run workflow** 选择 `test`，每个已配置渠道会收到一条测试消息。默认选项 `refresh` 只更新看板。发送结果记录在运行日志，服务商接受请求与终端实际收到消息可分别核对。

## 巡检时间与规则

北京时间周一至周五 **10:15、11:15、13:35、14:25** 计划巡检。最后一轮发全量日报。GitHub 的定时任务可能延迟或被丢弃，两次快照之间短暂越过目标价也可能错过。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

- 价位和指标在 `watchlist.json` 中修改。首档、二档及各项阈值沿用建仓库时提供的配置。页面默认按 `(现价 / 首档 − 1) × 100%` 升序排列，可按市场筛选、搜索代码，点名称查看原始时间和命中条件。
- 腾讯财经提供批量报价、前复权日 K 线，东方财富提供备用报价和指数行情。RSI(14) 使用 Wilder 平滑，盘中以最新价估算当日日线。MACD 柱仅供查看。512890 比较最近 60 个交易日（含当前）的最高收盘价，回撤超过 6% 或价格到档时列入观察。
- 159545 和 513530 先判断价位或 RSI，再核对 IOPV。溢价高于 1.5% 时发风控提示，IOPV 缺失或港股基准停更时暂停买点提醒。按 `(交易价 / IOPV − 1) × 100%` 计算溢价。
- 中国平安和招商银行的股息率用过去 365 天除息的每股现金股息合计除以现价。股息率观察线仅作对照，历史分红取不到时显示缺数。
- 场外 012708 优先使用当日已公布净值或带时间戳的盘中估值。第三方估值缺失时，仅用紧邻上一交易日的净值乘以 `1 + 931446 当日涨跌幅` 推算。申购按基金公司确认的净值结算。跟踪指数跌至 −1.5% 可单独触发。[中证指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208180546-931446_Index_Methodology_cn.pdf)
- 每个渠道实际发送前单独检查市场是否处于交易时段、基准行情是否更新、信号是否在最近 25 分钟内。内地 15:00 后只做收盘复盘，港股独立判断。旧行情、停牌和缺数会标注。两个行情源全部失败且无可用数据时，保留已发布页面。
- 同一天同一标的、条件、渠道只记录成功响应。首档后到二档可再发一条。回执按北京时间日期保存在 Actions Cache。缓存丢失、网络在服务商接收后中断或收件人部分拒收时，后续巡检可能重复发送。

## 本地运行

Python 3.11 及以上，仅需标准库。

```sh
python -m unittest discover -s tests -v
python monitor.py
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

本机打开 `http://127.0.0.1:8000`。也可以直接打开生成的 `docs/index.html`。最新机器可读快照在 `docs/snapshot.json`。

## 部署到另一个 GitHub 仓库

将源码推送到 `main`，在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。更新工作流中的 `DASHBOARD_URL` 及页面和 README 中的仓库链接，然后手动运行一次 `refresh`。工作流构建并发布 Pages artifact，定时执行无需写回源代码。[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
