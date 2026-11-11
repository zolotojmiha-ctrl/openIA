"""Safety of recovery diagnostics: private exceptions/stderr must stay private."""
import contextlib,importlib.util,io,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('recovery_diagnostic',root/'deploy/recovery/check.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class DiagnosticTests(unittest.TestCase):
    args=['--archive','/synthetic/archive.encrypted','--receipt','/synthetic/receipt.json']
    def test_private_exception_message_is_not_printed(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(m.verify_backup,'verify',side_effect=ValueError('PRIVATE-KEY-BODY-SHOULD-NOT-APPEAR')):
            self.assertEqual(m.main(self.args),1)
        self.assertNotIn('PRIVATE-KEY-BODY',output.getvalue())
        self.assertIn('ValueError',output.getvalue())
    def test_docker_error_is_categorized_without_stderr_or_arguments(self):
        def check(*a):
            m.verify_backup.subprocess.run(['docker','--host=unix:///var/run/docker.sock','run','PRIVATE-ARG'],stderr=subprocess.PIPE)
            raise RuntimeError('PRIVATE-SQL')
        result=subprocess.CompletedProcess([],1,stdout=b'',stderr=b'permission denied PRIVATE-TOKEN')
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(m.verify_backup,'verify',side_effect=check),patch.object(m.verify_backup.subprocess,'run',return_value=result):
            self.assertEqual(m.main(self.args),1)
        text=output.getvalue()
        self.assertIn('отказ в доступе',text)
        for value in ('PRIVATE-ARG','PRIVATE-SQL','PRIVATE-TOKEN'):self.assertNotIn(value,text)
    def test_success_delegates_to_verifier(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(m.verify_backup,'verify') as verify:
            self.assertEqual(m.main(self.args),0)
        verify.assert_called_once()
        self.assertIn('восстановление базы и файлов подтверждено',output.getvalue())

if __name__=='__main__':unittest.main()
