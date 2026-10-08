> 当前工作分支已支持单台电脑无需协调服务器运行。默认配置模板使用 `backend_mode=local`，查询、任务预留和永久跳过均在本机完成。聚水潭查询凭据需在本机另行配置。
> 详见 [单机版使用说明](单机版使用说明.md)。下文后台部署相关内容保留为旧版资料。

# 🖥️ 本地客户端

本目录集中存放桌面客户端、配置模板、依赖锁文件、运行与诊断脚本、Windows / macOS 构建工具及客户端使用说明。

## 配置与启动

从仓库根目录进入本目录，并复制配置模板：

```powershell
cd client
Copy-Item jst_operator_config.example.json jst_operator_config.json
```

默认单机模式无需后台地址或 API token。Windows 首次启动在“聚水潭凭据设置”窗口填写三项查询凭据，验证后使用 DPAPI 加密保存。

仅旧远程模式需填入 HTTPS 后台地址与 API token，且后台与客户端须匹配 API schema 5；token 为 32–128 字符，只使用英文字母、数字、下划线及连字符。

Windows x64 安装 CPython 3.10 AMD64，并确认提供 `py.exe`，然后双击 `Win10_21H1_源码直接启动.bat`。该入口使用 SHA256 锁定依赖，以不打印模式启动。

“不打印”仍可能执行改快递与取号，只能使用已批准的验收订单。

## 自检与回归

在本目录执行：

```sh
python jst_auto_print_app.py --self-test --self-test-output self-test.json
python -m unittest discover -s tests -p 'test_*.py'
```

`tests/` 同时覆盖客户端与服务端协议，执行全部回归时应保留仓库中的相邻 `../server/` 目录。

## 构建

| 平台 | 入口 |
| --- | --- |
| Windows x64 | `build_jst_auto_print_win10_x64.bat` |
| Windows Setup | `jst_auto_print_installer.iss`，由 Inno Setup 编译 |
| macOS ARM64 | `build_jst_auto_print_macos.sh` |

构建脚本仍以自身所在目录为工作目录。输出生成在本目录的 `dist_*` 下。运行模式配置会进入交付包；单机模式的聚水潭凭据另行保存在用户目录，不会进入安装包。旧远程配置含生产 token 的包只应提供给已授权电脑。

`vm_prepare_build_inputs.ps1` 是 Windows 构建机辅助工具，其源码目录指向 `C:\Users\Public\JSTBuild_V0525\jst-auto-print-assistant\client`；使用不同目录时先调整 `$Root`。

## 文件入口

- [桌面客户端](jst_auto_print_app.py)
- [使用说明](聚水潭安全打单助手_使用说明.md)
- [Windows 打包说明](Windows版打包说明.md)
- [Mac 使用说明](Mac版使用说明.md)
- [服务器端](../server/README.md)
- [项目总览](../README.md)

`jst_print_shadow_plan.py` 是供本地诊断及回归使用的只读规划模块；服务器部署使用 `../server/jst_print_shadow_plan.py`。
