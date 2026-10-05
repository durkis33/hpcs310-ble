from pathlib import Path
import sys
import unittest


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import lifecycle


class LifecycleContractTests(unittest.TestCase):
    def test_native_entrypoints_replace_powershell_lifecycle_scripts(self):
        for name in ('Install', 'Start', 'Stop', 'Restart', 'Approve', 'Run'):
            self.assertTrue((SOURCE / (name + '.cmd')).is_file())
        for name in ('Install', 'Start', 'Stop', 'Approve'):
            self.assertFalse((SOURCE / (name + '.ps1')).exists())
        self.assertNotIn('.ps1', (SOURCE / 'lifecycle.py').read_text(encoding='utf-8').lower())

    def test_startup_launcher_uses_cmd_host(self):
        install = Path(r'C:\Users\Test User\AppData\Local\PowerShellService')
        launcher = lifecycle.startup_launcher(install)
        self.assertEqual(launcher, '@echo off\r\ncall "C:\\Users\\Test User\\AppData\\Local\\PowerShellService\\Run.cmd" host\r\n')
        self.assertNotIn('.ps1', launcher.lower())


if __name__ == '__main__':
    unittest.main()
