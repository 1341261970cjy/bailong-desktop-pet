# 开发、验证与打包

本次提交基于 v0.5.1 WebM 源码；应用内部基础版本号仍为 `0.5.1`。本仓库当前提交不等于发布了新的 GitHub 二进制 Release。

## 已使用的环境

| 项目 | 版本 |
| --- | --- |
| 操作系统 | Windows x64 |
| Python | 3.12.14，64 位 |
| PySide6 | 6.9.2 |
| Pillow | 12.0.0 |
| psutil | 7.0.0 |
| PyInstaller | 6.22.3 |

依赖版本见 `requirements.txt`。macOS 源码和构建脚本尚未完成实机验收，不能由 Windows 构建结果推断 Mac 可用。

## 准备源码运行环境

在仓库根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

从[上游完整 v0.5.1 WebM 发布包](https://github.com/jingh12312-rgb/bailong-desktop-pet/releases/tag/v0.5.1)复制以下目录到仓库根目录：

```text
app/
outputs/
  白龙桌宠素材/
    素材清单.json
    …全部动画文件
tools/
  ffmpeg.exe
```

`outputs` 与 `tools` 是本地构建输入，不提交 Git。FFmpeg 需要支持透明 WebM 的 libvpx-vp9 解码；不要换成丢弃 alpha 通道的默认 VP9 解码方式。

```powershell
.venv\Scripts\python.exe app\pet.py
```

公开源码不包含个人爱给网音频。使用原点击声即可运行；通过产品声音菜单导入自己的文件，不应把个人导入目录复制进 Git。

## 测试与诊断

可先运行本次改动涉及的回归测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -s app -p test_station.py
.venv\Scripts\python.exe -m unittest discover -s app -p test_task_notifications.py
```

需要完整动画输入的离线界面和动画自检：

```powershell
.venv\Scripts\python.exe app\pet.py --self-test
```

安装版同样支持：

```powershell
.\BailongPet.exe --self-test
.\BailongPet.exe --self-test --station-only
```

`--station-only` 是真实只读网络查询，会读取本机已保存的站点设置。报告记录查询是否成功、字段是否返回及安全错误，不记录 API Key、用户访问令牌或实际余额数值。钱包成功而未配置账单令牌时，报告应明确今日查询尚未配置，不能当作今日金额已验收。

自检通过不能代替安装版联网检查。涉及 TLS 的打包改动必须在最终 EXE 中真实查询，不能仅凭源码 Python 请求成功下结论。配置凭据应通过本机“中转站设置…”输入，不要写入测试、命令行、截图或仓库。不要提交个人聊天、运行状态与诊断报告。

## 构建 Windows 完整包

安装构建工具：

```powershell
.venv\Scripts\python.exe -m pip install --target .build-tools pyinstaller==6.22.3
.venv\Scripts\python.exe work\build_windows.py
.venv\Scripts\python.exe work\build_release.py
```

`build_windows.py` 生成 `发布包/build-v0.5.1/BailongPet`，`build_release.py` 将运行库、FFmpeg、动画、声音及说明整理为便携目录和 ZIP，并生成校验文件。版本路径由 `app/version.py` 决定。构建整理前先退出正在运行的目标 EXE，以免文件被锁定。

解码器优先使用 `work/video-deps/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`，该路径不存在时使用 `tools/ffmpeg.exe`。必须保留素材清单与所有引用的动画。

### HTTPS 依赖

当前构建脚本显式收集同一 Python 安装中 `sys.base_prefix/DLLs` 下的 `libssl-3-x64.dll`、`libcrypto-3-x64.dll`，并校验输出哈希。PATH 中其他软件（例如 PDF 工具）可能提供不同版本 OpenSSL，不能混入 Python 的运行依赖。若构建环境不含脚本要求的 DLL，应先核对 Python 发行环境与依赖名称，不要随意下载其他软件的 DLL 替换。

本次使用的 Python 运行环境对 `ssl.py` 做了证书策略扩展，初始化时会加载旁边的 `codex-rejected-cas.pem`。脚本在检测到该文件时将它加入 EXE 的 `_internal` 根目录并校验内容。缺失该文件会让 HTTPS 在连接前抛出 `FileNotFoundError`，表现为安装版无法查询额度。

保留 Python 自带的验证和证书策略；不要用关闭证书验证的方法掩盖打包缺失。使用不同 Python 发行环境时，以该环境的实际 TLS 依赖为准。

## DogAPI 数据口径

- API Key 查询账户钱包及当前 Key 用量；累计用量只作内部解析，界面不显示。
- 今日消耗使用用户 Access Token 认证的 `/api/log/self/stat`，带数字账号 ID，按固定 UTC+8 的当天零点汇总账户消费日志。
- 普通 Key 的 `/api/log/token/` 最近记录最多 1000 条，不能视作完整当天数据。
- 单位换算依据站点返回的显示比例或公开配置；无可靠单位时应显示不可查询，不猜金额。
- 今日统计失败不应丢弃有效钱包数据，旧凭据请求结果不能覆盖新配置的数据。
- 额度、互动与工作汇报各自维护显示状态。额度按屏幕空间固定在桌宠左侧或右侧；互动和汇报使用独立位置，临时面板开关不应挤动其他面板。布局测试应覆盖屏幕边缘、负坐标屏幕与面板显隐全过程。
- 完成汇报短内容约 15 秒、长内容约 30 秒后收起；进展提示在最后更新约 15 秒后收起。普通互动不能取消汇报计时或覆盖其内容，完整结果保留在聊天历史与详情中。数值展示和数据解析应分别测试。

## 发布检查

提交源码前检查差异中没有凭据、聊天和运行文件，也没有本地 FFmpeg、动画或个人音频。验证 ZIP 中有必需的运行库、素材和说明，且不夹带账户数据。公开上传新安装包是独立发布步骤，应在发布说明中明确包含哪些定制修改；不能把上游旧包标成新包。
