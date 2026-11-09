import unittest
from unittest.mock import Mock, patch

import autostart


class AutostartTests(unittest.TestCase):
    def test_detects_installed_codex_windows_path(self):
        process=Mock()
        process.info={'name':'ChatGPT.exe','exe':r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe'}
        with patch.object(autostart.psutil,'process_iter',return_value=[process]):
            self.assertTrue(autostart.gpt_is_open())

    def test_startup_switch_reflects_entry_file(self):
        entry=Mock()
        entry.is_file.side_effect=(True,False)
        with patch.object(autostart,'startup_entry',return_value=entry):
            self.assertTrue(autostart.autostart_enabled())
            self.assertFalse(autostart.autostart_enabled())


if __name__=='__main__':unittest.main()
