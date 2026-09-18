# 中乙联赛 APTV 自动订阅

按官方全赛季赛程抓取中乙比赛，只发布已经验证可读取的视频源。1080P 优先；未公布或暂不可播放的比赛保留在赛程表中。

## 订阅

在 APTV 添加远程 M3U 订阅，并开启客户端自动刷新：

- 最佳画质：`https://ricadre.github.io/zhongyi-live/zhongyi.m3u`
- 所有可用画质：`https://ricadre.github.io/zhongyi-live/zhongyi-all.m3u`
- 赛程及状态：<https://ricadre.github.io/zhongyi-live/>

## 自动更新

GitHub Actions 每 15 分钟触发一次；GitHub 可能排队延迟。手机/电视仍需刷新订阅才能读到新列表。每次读取官方全赛季赛程，检查未来 7 天及仍在进行的比赛，再获取单场官方画质列表。每条源都检查 M3U8 和少量视频分片，验证成功才发布；默认每场只选最高可用画质。比赛结束会移出播放列表。没有正在直播的比赛时列表可能为空，赛程页仍可查看后续安排。

上游赛程整体不可用或在播比赛的视频源全部检查失败时，本次发布会失败，保留上一次成功版本。状态页标有最近成功更新时间，不能把旧列表当成实时可用保证。每天保存一份赛程快照到 `data/`，也让自动任务所在仓库保持活动。

不需要个人 Cookie、密码、会员凭据或云服务器常驻。GitHub 仅提供小型播放列表，视频由原直播 CDN 直接传输。本项目不解密、不代理、不绕过付费或登录限制。源站规则改变后可能需要维护。

## 本地运行

需要 Python 3.12 或以上，不依赖第三方 Python 包。

```sh
python -m unittest discover -p 'test_*.py' -v
python update.py --output site
```

## 数据依据

- 官方全赛季赛程：`stats.qiumibao.com/shuju/public/index.php`，中乙 `league_id=355`。
- 官方即时比分：`matchs.qiumibao.com/live/all.htm`。
- 官方单场 App 数据：`s.qiumibao.com/m/ios/json/` 后接赛程中实际内页路径。
- 只接收官方频道中已观测的 CDN 主机，RTMP 对应 HLS 地址必须经过实际验证。

接口没有稳定性承诺；可能变更、延迟或对不同网络有限制。

## 搜索引擎

首页带有 `noindex, nofollow`，请求搜索引擎不要收录或跟随链接。域名根目录的 `https://ricadre.github.io/robots.txt` 对本项目的 M3U、JSON 文件禁止抓取；该规则由单独的 `Ricadre.github.io` 用户站点提供，放在本项目子目录下不会生效。首页保留可抓取，以便搜索引擎读取 noindex。

这些是供守规则的爬虫遵循的指令，不是身份验证。网站、订阅地址与本公开仓库仍可直接访问；已有搜索结果消失也需要搜索引擎重新处理。
