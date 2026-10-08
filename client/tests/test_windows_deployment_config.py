"""Run the actual staging configuration validator under PowerShell StrictMode."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(POWERSHELL, 'requires Windows PowerShell or pwsh')
class WindowsDeploymentConfigTests(unittest.TestCase):
    def test_local_legacy_and_remote_configs_under_strict_mode(self):
        source = (ROOT / 'verify_jst_win10_build.ps1').read_text(encoding='utf-8-sig')
        functions = []
        for name in ('Assert-True', 'Assert-DeploymentConfig'):
            match = re.search(r'(?ms)^function ' + re.escape(name) + r'\s*\{.*?(?=^function )', source)
            self.assertIsNotNone(match)
            functions.append(match.group())
        script = '''
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
''' + '\n'.join(functions) + '''
foreach ($text in @(
    '{"backend_mode":"local","debug_port":9222,"loop_seconds":5}',
    '{"debug_port":9222,"loop_seconds":5}',
    '{"api_url":"https://example.test/api","api_token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","debug_port":9222,"loop_seconds":5}',
    '{"backend_mode":"remote","api_url":"https://example.test/api","api_token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","debug_port":9222,"loop_seconds":5}'
)) {
    Assert-DeploymentConfig ($text | ConvertFrom-Json)
}
foreach ($text in @(
    '{"backend_mode":"remote","debug_port":9222,"loop_seconds":5}',
    '{"backend_mode":"invalid","debug_port":9222,"loop_seconds":5}',
    '{"backend_mode":"local","debug_port":80,"loop_seconds":5}',
    '{"backend_mode":"local","debug_port":9222,"loop_seconds":1}',
    '{"backend_mode":"local"}',
    '{"api_url":"https://example.test/api","debug_port":9222,"loop_seconds":5}'
)) {
    $rejected = $false
    try { Assert-DeploymentConfig ($text | ConvertFrom-Json) }
    catch { $rejected = $true }
    if (-not $rejected) { throw "invalid configuration was accepted" }
}
Write-Output "STRICTMODE CONFIG PASS"
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config-test.ps1'
            path.write_text(script, encoding='utf-8-sig')
            result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive',
                                     '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STRICTMODE CONFIG PASS', result.stdout)
