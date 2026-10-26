import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    base = ROOT.parent if name == ".gitignore" or name.startswith("server/") else ROOT
    return (base / name).read_text(encoding="utf-8-sig")


def _parse_hash_lock(name: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in _read(name).splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        raise AssertionError(f"unterminated requirement in {name}: {pending}")

    result: dict[str, tuple[str, tuple[str, ...]]] = {}
    for line in logical_lines:
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)==([^ ]+)((?: +--hash=sha256:[0-9a-f]{64})+)",
            line,
        )
        if not match:
            raise AssertionError(f"non-exact or unhashed requirement in {name}: {line}")
        package = match.group(1).lower().replace("_", "-")
        hashes = tuple(re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group(3)))
        if package in result:
            raise AssertionError(f"duplicate locked package in {name}: {package}")
        result[package] = (match.group(2), hashes)
    return result


class BuildSupplyChainTests(unittest.TestCase):
    def test_pyinstaller_invocation_cannot_execute_extra_python_files(self):
        script = _read("build_jst_auto_print_win10_x64.bat")
        start = script.index('"%BUILD_VENV%\\Scripts\\python.exe" -m PyInstaller')
        end = script.index('if errorlevel 1 (', start)
        command_lines = [line.rstrip() for line in script[start:end].splitlines() if line.strip()]
        # Every line except the last must continue the SAME command. Otherwise
        # CMD runs a stray .py through file associations (possibly Python 3.12).
        for line in command_lines[:-1]:
            self.assertTrue(line.endswith('^'), f'premature command terminator: {line}')
        self.assertEqual(command_lines[-1].strip(), '"jst_auto_print_app.py"')
        self.assertEqual(sum(line.strip().endswith('.py"') for line in command_lines), 1)

    def test_windows_build_lock_is_complete_exact_and_hashed(self):
        lock = _parse_hash_lock("jst_auto_print_requirements_win10_x64.txt")
        expected = {
            "pip": "26.2",
            "setuptools": "84.0.0",
            "altgraph": "0.17.5",
            "packaging": "26.3",
            "pefile": "2023.2.7",
            "pyinstaller-hooks-contrib": "2026.7",
            "pywin32-ctypes": "0.2.3",
            "pyinstaller": "6.22.2",
            "websocket-client": "1.9.0",
            "tzdata": "2026.5",
        }
        self.assertEqual({package: value[0] for package, value in lock.items()}, expected)
        self.assertTrue(all(len(hashes) >= 1 for _, hashes in lock.values()))
        self.assertEqual(
            lock["pip"][1],
            ("931c303696af6fa3417112103b1cad26890e5a07eccb5b99783700e33f2b8aad",),
        )

    def test_windows_source_runtime_lock_is_complete_exact_and_hashed(self):
        lock = _parse_hash_lock("jst_auto_print_runtime_requirements_win10_x64.txt")
        expected = {
            "pip": "26.2",
            "setuptools": "84.0.0",
            "websocket-client": "1.9.0",
            "tzdata": "2026.5",
        }
        self.assertEqual({package: value[0] for package, value in lock.items()}, expected)
        self.assertTrue(all(len(hashes) >= 1 for _, hashes in lock.values()))
        self.assertEqual(
            lock["pip"][1],
            ("931c303696af6fa3417112103b1cad26890e5a07eccb5b99783700e33f2b8aad",),
        )

    def test_build_batch_uses_pinned_hashed_dependencies_and_file_parameters(self):
        script = _read("build_jst_auto_print_win10_x64.bat")
        lower = script.lower()
        self.assertNotIn("-command", lower)
        self.assertNotIn("-encodedcommand", lower)
        self.assertNotRegex(lower, r"pip +install +(?:-u|--upgrade)")
        self.assertIn("--require-hashes", script)
        self.assertIn("--only-binary=:all:", script)
        self.assertIn("--no-cache-dir", script)
        self.assertIn("'pip':'26.2'", script)
        self.assertIn("'pyinstaller':'6.22.2'", script)
        self.assertIn("'websocket-client':'1.9.0'", script)
        self.assertIn("'tzdata':'2026.5'", script)
        self.assertIn("--collect-all tzdata", script)
        self.assertIn("for d in m.distributions()", script)
        self.assertIn("assert actual==expected", script)
        self.assertIn("--contents-directory \".\"", script)
        self.assertIn("-File \"%~dp0build_jst_artifacts.ps1\"", script)
        self.assertIn('-SourceRoot "%~dp0."', script)
        self.assertNotIn('-SourceRoot "%~dp0"', script)
        self.assertIn("APP_VERSION')=='0.5.25'", script)
        self.assertIn("API_SCHEMA_VERSION')==5", script)
        self.assertIn("PLANNER_SCHEMA_VERSION')==5", script)
        self.assertIn("re.fullmatch(r'[A-Za-z0-9_-]{32,128}'", script)
        self.assertNotIn("len(str(c.get('api_token','')))>=32", script)
        self.assertIn("JSTAutoPrint_Win10_21H1_V0.5.25.zip", script)

    def test_source_batch_is_hash_locked_without_floating_pip_upgrade(self):
        script = _read("Win10_21H1_源码直接启动.bat")
        lower = script.lower()
        self.assertNotIn("-command", lower)
        self.assertNotIn("-encodedcommand", lower)
        self.assertNotRegex(lower, r"pip +install +(?:-u|--upgrade)")
        self.assertIn("jst_auto_print_runtime_requirements_win10_x64.txt", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("--only-binary=:all:", script)
        self.assertIn("'pip':'26.2'", script)
        self.assertIn("-Operation CleanRuntime", script)
        self.assertIn("'tzdata':'2026.5'", script)
        self.assertIn('-SourceRoot "%~dp0."', script)
        self.assertNotIn('-SourceRoot "%~dp0"', script)
        self.assertIn("for d in m.distributions()", script)
        self.assertIn("assert actual==expected", script)
        self.assertIn("APP_VERSION')=='0.5.25'", script)
        self.assertIn("API_SCHEMA_VERSION')==5", script)
        self.assertIn("re.fullmatch(r'[A-Za-z0-9_-]{32,128}'", script)
        self.assertNotIn("len(str(c.get('api_token','')))>=32", script)

    def test_all_batch_files_avoid_inline_powershell_commands(self):
        for path in ROOT.glob("*.bat"):
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
                text = raw.decode("utf-8-sig").lower()
                self.assertNotIn("powershell -command", text)
                self.assertNotIn("powershell.exe -command", text)
                self.assertNotIn("-encodedcommand", text)

    def test_offline_check_does_not_mutate_manifest_protected_payload(self):
        script = _read("Win10_离线自检.bat")
        self.assertIn("%USERPROFILE%\\.jst-auto-print\\diagnostics", script)
        self.assertNotIn("%~dp0Win10_离线自检.log", script)

    def test_backend_network_diagnostic_is_safe_complete_and_packaged(self):
        script = _read("Win10_后台网络诊断.bat")
        for required in (
            "jst_operator_config.json",
            'more +%PAYLOAD_LINE% "%~f0"',
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP_PS%"',
            "GetHostAddresses",
            "TcpClient",
            "AllowAutoRedirect = $false",
            'UserAgent.ParseAdd("JSTAutoPrint/0.5.25")',
            'AuthenticationHeaderValue]::new("Bearer", $apiToken)',
            'StringContent]::new("{}"',
            "api_schema_version",
            "lease_required",
            "WScript.Shell",
            ".jst-auto-print\\diagnostics",
            "内容不写入日志",
            "后台基础连接正常",
            "本检查不会调用可能领取订单的 /plan",
        ):
            self.assertIn(required, script)
        self.assertNotIn("Write-Host $apiToken", script)
        self.assertNotIn("Add-LogLine $apiToken", script)

        build = _read("build_jst_auto_print_win10_x64.bat")
        verifier = _read("verify_jst_win10_build.ps1")
        installer = _read("jst_auto_print_installer.iss")
        self.assertIn("Win10_后台网络诊断.bat", build)
        self.assertIn("Win10_后台网络诊断.bat", verifier)
        self.assertNotIn("diagnose_jst_backend.ps1", build)
        self.assertNotIn("diagnose_jst_backend.ps1", verifier)
        self.assertIn("Win10_后台网络诊断.bat", installer)

    def test_vm_input_preparation_targets_current_release(self):
        script = _read("vm_prepare_build_inputs.ps1")
        self.assertIn("JSTBuild_V0525", script)
        self.assertIn("jst-auto-print-assistant\\client", script)
        self.assertIn('"_V0.5.25_"', script)
        self.assertNotIn("JSTBuild_V0521", script)
        self.assertNotIn("V0.5.21_Windows_SourceRuntime", script)

    def test_retired_fast_builder_cannot_execute_a_build(self):
        script = _read("build_jst_fast_v0514.bat")
        executable_lines = [
            line.strip().lower()
            for line in script.splitlines()
            if line.strip() and not line.lstrip().lower().startswith("echo ")
        ]
        self.assertIn("exit /b 64", executable_lines)
        self.assertFalse(
            any(
                re.search(r"\b(?:call|start|python|pyinstaller|pip|powershell)\b", line)
                for line in executable_lines
            )
        )

    def test_artifact_helper_has_scoped_cleanup_and_exact_manifest(self):
        script = _read("build_jst_artifacts.ps1")
        for required in (
            "Resolve-Path -LiteralPath $SourceRoot",
            "StartsWith($RootPrefix",
            "Assert-NoReparseAncestor",
            "[IO.FileAttributes]::ReparsePoint",
            "[IO.Path]::IsPathRooted",
            "'[:\\x00-\\x1F]'",
            '$segment -ne ".."',
            "case-insensitive duplicate staged path",
            "$MaxEntryBytes",
            "$MaxTotalBytes",
            "Get-FileHash -LiteralPath",
            "SHA256SUMS.txt",
            "CreateFromDirectory",
            "JSTAutoPrint_Win10_21H1_V0.5.25.zip",
        ):
            self.assertIn(required, script)
        self.assertNotIn("Invoke-Expression", script)

    def test_zip_verifier_rejects_ambiguous_or_dangerous_archives(self):
        script = _read("verify_jst_win10_build.ps1")
        for required in (
            "[IO.Path]::IsPathRooted",
            "'[:\\x00-\\x1F]'",
            '$segment -ne ".."',
            "OrdinalIgnoreCase",
            "ExternalAttributes",
            "ReparsePoint",
            "file/directory path collision",
            "file shadows a parent directory",
            "$MaxArchiveEntries",
            "$MaxEntryBytes",
            "$MaxTotalBytes",
            "$MaxManifestBytes",
            "actual read limit",
            "actual hash limit",
            "actual total uncompressed size limit",
            "SHA256 mismatch",
            "does not exactly match the manifest",
            "files do not exactly match the payload manifest",
            "RequireAuthenticode",
            "is not publisher authentication",
            "AssertNoRunningInstance",
            "^[A-Za-z0-9_-]{32,128}$",
        ):
            self.assertIn(required, script)

    def test_docs_are_version_consistent_sanitized_and_honest_about_trust(self):
        docs = "\n".join(
            _read(name)
            for name in (
                "聚水潭安全打单助手_使用说明.md",
                "聚水潭安全打单助手_V0.5.25_运行逻辑与流程图.md",
                "Windows版打包说明.md",
                "Windows源码运行包_先读.txt",
                "server/DEPLOYMENT_V0.5.21.md",
            )
        )
        self.assertNotIn("V0.5.20", docs)
        self.assertNotIn("V0.5.22", docs)
        self.assertNotIn("API schema 3", docs)
        self.assertNotIn("API schema 4", docs)
        self.assertIn("API schema 5", docs)
        self.assertIn("waybill_fingerprint", docs)
        self.assertNotIn("minimum_client_version=0.5.20", docs)
        self.assertNotRegex(docs, r"\b(?:6789689|6790800|13542227|13542224)\b")
        self.assertIn("9000001/19000001", docs)
        self.assertIn("微软安全支持期", docs)
        self.assertIn("不能证明发布者身份", docs)
        self.assertIn("SHA256SUMS.txt", docs)

    def test_generated_and_sensitive_paths_are_gitignored(self):
        ignores = _read(".gitignore")
        for pattern in (
            ".build-venv-win10/",
            ".runtime-venv-win10/",
            ".pyinstaller-work-win10/",
            ".pyinstaller-spec-win10/",
            "dist_win10_x64/",
            "*.zip",
            "*.log",
            "*.db",
            ".env",
        ):
            self.assertIn(pattern, ignores)


if __name__ == "__main__":
    unittest.main()
