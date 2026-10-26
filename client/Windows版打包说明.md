# 聚水潭安全打单助手 V0.5.25：Windows 构建与交付

当前分支支持单机模式。源码和回归检查不能替代 Windows 上的实际构建、安装及实体打印验收；本说明不表示已生成并验收当前分支的成品。

## 构建环境

- 在仍处于微软安全支持期内的 Windows x64 电脑构建。现有 BAT 和完整依赖锁固定要求 CPython 3.10 AMD64、Python Launcher（`py.exe`）、pip、venv 和 Tcl/Tk；无需 conda。
- 当前工具链仍固定 Python 3.10。该系列已结束官方支持，升级构建 Python 需同步重新解析依赖锁及验收，不要自行跳过版本和哈希检查。
- 生成 Setup 时使用 Inno Setup 6.3 或更新版本，并确认编译器中存在 `Languages\ChineseSimplified.isl`。脚本使用默认安装器架构封装 x64 程序，保持 `ArchitecturesAllowed=x64compatible` 和 64 位安装模式，不依赖 Inno Setup 7 的 `SetupArchitecture` 指令。

## 准备配置

完整解压源码，进入 `client` 目录。将 `jst_operator_config.example.json` 复制为 `jst_operator_config.json`，单机模式使用：

```json
{
  "backend_mode": "local",
  "debug_port": 9222,
  "loop_seconds": 5
}
```

该文件不会提交到 Git，因此新下载的源码需自行复制。它会进入交付包。`backend_mode=local` 不要求后台地址或 Bearer Token；旧远程模式仍校验 HTTPS 后台地址和 API token。API schema 5 / planner schema 5 的内部契约继续保留。

聚水潭真实 `app_key`、`appsecret`、`access_token` 不放入上述打包配置。使用电脑单独配置 `%USERPROFILE%\.jst-auto-print\jst_openapi_config.json` 或对应 `JST_*` 环境变量。程序没有凭据输入窗口、加密存储或自动 Token 刷新。详见 [单机版使用说明](单机版使用说明.md)。

## 生成程序和 ZIP

双击 `build_jst_auto_print_win10_x64.bat`。BAT 自动创建独立构建环境并安装完整 SHA256 锁定依赖，使用 `--require-hashes --only-binary=:all:` 和 PyInstaller onedir 模式。不要使用已退役的 `build_jst_fast_v0514.bat`。

Windows 构建和源码运行依赖锁都包含固定版本及 SHA256 的 `tzdata`。PyInstaller 显式收集该包，安装后无需额外安装时区库；构建阶段会确认 `Asia/Shanghai` 可用。Windows 通常没有系统 IANA 时区数据库，缺少此依赖会导致 `ZoneInfoNotFoundError`。不要通过改成 UTC 或忽略错误来绕过业务时区检查。

构建入口先检查全部单机源文件及配置，随后检查版本、依赖、PE AMD64 架构、暂存 EXE 离线自检、ZIP 内容和 `SHA256SUMS.txt`。离线自检要求 `tkinter=true`、`native_cdp=true`、`local_runtime=true` 和 `self_test=PASS`；其中 `local_runtime` 加载本机规划、协调、数据源、OpenAPI 和预留模块，并仅在临时数据库中检查永久排除持久性。它不读取真实查询凭据、不发起聚水潭请求、不启动打印动作，也不修改用户的任务数据库。

任一步失败都会中止构建。出现 `BUILD AND ZIP VERIFICATION PASSED` 后，ZIP 位于：

```text
dist_win10_x64\JSTAutoPrint_Win10_21H1_V0.5.25.zip
```

SHA256 文件位于同目录，ZIP 文件名后附 `.sha256`。失败时查看 `build_win10_x64.log` 最后对应步骤的具体错误，不要忽略校验后继续交付。

## 生成 Setup

仅在上述构建门禁通过后，用 Inno Setup 打开 `jst_auto_print_installer.iss` 并编译。脚本读取本次构建的 `dist_win10_x64\JSTAutoPrint_Win10_21H1`，不要使用旧暂存目录替代。现有 BAT 生成程序与 ZIP，不自动调用 Inno Setup。

输出文件：

```text
dist_win10_installer\JSTAutoPrint_Windows_x64_V0.5.25_Setup.exe
```

Setup 或完整便携包包含 Python 运行环境，使用电脑无需安装 Python 或 conda。安装包不包含聚水潭真实查询凭据，浏览器、聚水潭打印组件、打印驱动和打印机仍需在使用电脑准备。

## 安装后验收

1. 关闭旧助手，保留 `%USERPROFILE%\.jst-auto-print` 中的历史任务与 SKU 数据；切换单机模式前核对服务器独有的永久跳过记录。
2. 完整解压 ZIP 或安装 Setup，不能仅复制主 EXE。
3. 准备本机聚水潭凭据，确认程序处于单机模式。
4. 启动助手，在专用 Chrome/Edge 中登录聚水潭，打开打单拣货页并启动本地打印组件。安装后的主程序不会自动开始打单。
5. 运行包内 `Win10_离线自检.bat`；它先检查 EXE 离线自检，再检查浏览器/CDP 和本地打印服务。输出写入用户数据目录，不修改受清单保护的安装目录。仅需构建阶段检查时，使用打包 BAT 内部的 `-ExecutableSelfTest` 检查。
6. `Win10_后台网络诊断.bat` 仍面向旧远程 HTTP 后端，不适用于证明单机版聚水潭查询可用。单机接口验证可使用主 EXE 的 `--live-readonly-probe --probe-output <输出路径>`，它要求有效凭据、已登录的专用浏览器和打单页面，不改快递、不取号、不打印。
7. 选择与实体纸张匹配的面单类型。`--no-print` 仍可能真实改快递和取号，只能用于已批准的验收订单。正式运行前完成一单实体纸张、条码、运单和 SKU 核对。

## 数据与交付边界

按 `o_id + io_id` 精确确认任务、预留、仓库和动作结果；`waybill_fingerprint` 防止只比较运单后四位。示例 `9000001/19000001` 仅用于说明，不是实际验收订单。RUNNING 任务只读恢复，不自动重放；不调用预发货/发货接口。

`SHA256SUMS.txt` 可发现文件损坏，但不能证明发布者身份。没有 Authenticode 签名时不能称为已签名。不要把真实查询凭据和任务数据库放入安装目录或交付包。

## 源码备用入口

`Win10_21H1_源码直接启动.bat` 会验证哈希锁定依赖、源码版本、单机/远程配置及单机源文件，再以不打印模式启动。单机模式无需填原后台地址和 Token；不要清空已有任务数据库。该入口需要本机 Python 3.10 x64，与安装后直接运行 EXE 的方式不同。
