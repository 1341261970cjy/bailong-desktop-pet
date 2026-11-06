# 白龙桌宠 · 定制更新版源码

基于白龙桌宠 v0.5.1 WebM 版，增加聊天界面调整、DogAPI 钱包与今日消耗查询、独立提示面板和本地声音导入。保留 35 段透明动画、自移动、吸边、大小调节与 Codex 公开任务提醒。

- 当前仓库：[1341261970cjy/bailong-desktop-pet](https://github.com/1341261970cjy/bailong-desktop-pet)
- 普通操作与配置：[使用说明](使用说明.md)
- 环境、测试与打包：[开发说明](DEVELOPMENT.md)
- 本次修改：[更新记录](RELEASE_NOTES.md)

**本次更新提供源码，尚未发布新的定制版 GitHub Release。** GitHub 的“Code → Download ZIP”是源码包，不能直接双击运行。原作者 v0.5.1 的旧 EXE 不包含这里后续的定制功能。

## 本次功能

- 鼠标悬停显示“聊天 / 额度 / 声音 / ⋯”，按钮跟随桌宠移动，贴近屏幕底部时避开角色；更多选项集中在右键菜单。
- 聊天窗口采用紫白圆角界面、自定义最小化/最大化/关闭按钮，支持会话列表、新建、任务目录与本机历史。
- 额度面板采用紧凑布局和更易读的字体，金额保留三位小数，不显示更新时间。标题右侧的圆圈用于刷新，查询时转动，不另占一行提示。增加最近调用的首字等待与总耗时参考。
- Codex 与 DogAPI 卡片可分别开关；额度保留当前左侧或右侧，只有该侧放不下时才换边。互动提示先定位再显示，工作汇报靠近桌宠头侧，减少遮挡、跳位和多余留白。
- 完成汇报短内容约 15 秒、长内容约 30 秒后自动收起；进展提示在最后更新约 15 秒后收起。完整结果仍可从聊天历史或详情查看。
- DogAPI 显示账户钱包余额和今日消耗，移除累计消费及 Key 剩余额度。
- 声音每次点击只播放一遍，任务完成不会额外触发点击声；支持试听、静音及本地音频导入。
- 修复 Windows 打包时 HTTPS 运行库和证书策略文件缺失导致的额度查询失败。

## 运行当前源码

当前验证环境为 Windows x64、Python **3.12.14**。

1. 安装 64 位 Python，在项目目录建立环境并安装依赖：

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. 从[原作者 v0.5.1 完整 WebM 发布包](https://github.com/jingh12312-rgb/bailong-desktop-pet/releases/tag/v0.5.1)复制整个 `outputs` 和 `tools` 文件夹到项目根目录，与 `app` 并列。需要全部动画、素材清单和 `tools/ffmpeg.exe`；这些大文件未纳入 Git。
3. 启动：

   ```powershell
   .venv\Scripts\python.exe app\pet.py
   ```

完整 Windows 包自带 Python 和 Qt，但需要自行构建或等待对应定制版本的正式发布。构建步骤见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## DogAPI 配置

右键桌宠 →“中转站设置…”，地址填写 `https://www.dogapi.cc`。

| 数据 | 所需设置 |
| --- | --- |
| 钱包余额 | 模型调用 API Key，通常以 `sk-` 开头 |
| 完整今日消耗 | 另外填写网站用户 Access Token 与数字账号 ID |

账号 ID 位于网站个人资料姓名旁；用户令牌位于个人资料 → Security（安全）→ Access Token。**网站打开 Access Token 项会重新生成令牌，可能使旧令牌失效。**

今日消耗读取日志的完整统计接口，按北京时间（Asia/Shanghai）当天 00:00 至查询时刻汇总。普通 API Key 的最近 1000 条日志不足以保证全天完整，程序不会将累计金额或截断记录当成今日总额。未配置账单权限时会提示，钱包可单独查询。DogAPI 开启时每分钟刷新，也可手动刷新。

凭据只填写并保存在本机设置，勿写入源码、聊天或 GitHub。

## 声音与个人数据

公开仓库只提供原点击声，支持从“声音 → 添加或更换本地音效…”导入 WAV、MP3、OGG、FLAC。个人导入的爱给网小黄鸭和其他个人音频不随 Git 公开。

当前 Windows 导入音频保存到 `D:\CodexDragonBuild\UserData\sounds`；D 盘不可用时会提示。聊天历史、API Key、账单访问令牌和运行诊断不属于公开源码。

## 适用范围

没有 Codex 也能使用普通动画、移动和吸边。聊天、Codex 官方额度及任务同步需要使用者自己的 Codex 安装与登录；DogAPI 使用独立的站点凭据。

macOS 适配源码尚未完成实机验收，不提供已验证的 Mac 安装包；Linux、32 位 Windows 和 Windows ARM 未验证。

本项目非 OpenAI 官方产品。源码尚未指定开源许可证，美术素材说明见 [ASSETS.md](ASSETS.md)，运行库遵循各自许可证。
