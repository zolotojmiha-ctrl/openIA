"""Every old deploy/hotfixes entry point refuses before any side effect. Synthetic isolated tree."""
import hashlib,os,stat,subprocess,tempfile,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
HOTFIXES=sorted((REPO/'deploy/hotfixes').glob('*.sh'))


class HotfixChannelTests(unittest.TestCase):
    def test_inventory_is_complete(self):
        historical={p.name[:-len('.disabled')] for p in (REPO/'deploy/hotfixes/historical').glob('*.disabled')}
        self.assertEqual({p.name for p in HOTFIXES},historical,'every old entry point has a refusing stub and a preserved historical copy')
        self.assertEqual(len(HOTFIXES),22)
        for p in (REPO/'deploy/hotfixes/historical').glob('*.disabled'):self.assertFalse(p.stat().st_mode & stat.S_IXUSR,'historical copies are not executable')

    def test_all_entry_points_refuse_without_side_effects(self):
        for script in HOTFIXES:
            with self.subTest(script=script.name),tempfile.TemporaryDirectory() as td:
                root=Path(td);app=root/'app';(app/'deploy/beget').mkdir(parents=True);(app/'deploy/beget/compose.yml').write_text('x');(app/'index.html').write_text('OLD')
                (root/'manifest.json').write_text('{}');log=root/'calls.log';bin=root/'bin';bin.mkdir()
                for name in ('docker','curl','wget','sudo','systemctl','python3','sha256sum','cp','mv','rm','tar','unzip'):
                    (bin/name).write_text('#!/bin/sh\necho %s >> "%s"\nexit 99\n'%(name,log));(bin/name).chmod(0o755)
                before={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p.parent!=bin}
                copy=root/script.name;copy.write_text(script.read_text());copy.chmod(0o755)
                result=subprocess.run(['/bin/bash',str(copy)],env={'PATH':str(bin),'ARMADA_APP_ROOT':str(app)},capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,3,result.stdout+result.stderr)
                self.assertIn('закрыт',result.stderr);self.assertIn('подписанный накопительный выпуск',result.stderr)
                self.assertFalse(log.exists(),'no external command may be started')
                after={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p.parent!=bin and p.name!=script.name}
                self.assertEqual({k:v for k,v in before.items()},{k:v for k,v in after.items() if k in before})

    def test_stubs_contain_no_executable_logic(self):
        for script in HOTFIXES:
            text=script.read_text()
            for forbidden in ('docker','backup.sh','curl','manifest.json','unzip','python3','rm ','cp '):
                self.assertNotIn(forbidden,text.replace('docker compose','').replace('Docker','') if forbidden=='docker' else text,script.name+': '+forbidden)
            self.assertEqual(len([l for l in text.splitlines() if l and not l.lstrip().startswith(('#',"'"))]),2,script.name)


if __name__=='__main__':unittest.main(verbosity=2)
