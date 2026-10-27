# 风险告警  2026-10-27 13:20:03

高危 1 · 关注 1 · 提示 1 · 正常 12

- **[高危] R08 常驻服务掉线** — cs-lab｜常驻服务不在跑，页面/基准会停更
  - 处置：`systemctl restart cs-lab`
- **[关注] R12 基准组失败残留** — 8/41 组｜4090@cn-bj2-03/flux_fast: ERR:ssh: connect to host cpod-1wxwqrk1sy；4090_48G@cn-wlcb-01/sd15_base: mport: command not found
/root/lab/bench；4090_48G@cn-wlcb-01/sd15_fast: mport: command not found
/root/lab/bench
  - 处置：`看 reports/lab/ 下的失败明细`
