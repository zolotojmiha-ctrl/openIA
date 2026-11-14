"""Packaging boundaries: runtime code/licences, no nested past release artifacts."""
import io,zipfile,tempfile,sys,json,importlib.util,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('package_release',ROOT/'deploy/beget/package_release.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PackagingTests(unittest.TestCase):
    def test_package_keeps_licence_and_wheels_but_excludes_old_release_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'out';buf=io.BytesIO()
            files=['README.md','RELEASE_6_33.md','THIRD_PARTY_NOTICES.md','AGENTS.md','RELEASE_6_32_VALIDATION.md','armada-old-fix.zip','armada-old-fix.zip.sig','old-source.bundle','tests/test_example.py','server/config/settings.py','deploy/synology/wheels/test.whl']
            with zipfile.ZipFile(buf,'w') as z:
                for name in files:z.writestr('openIA-feature-accounts-foundation/'+name,'TEST')
            def git(*args):
                if args[0]=='status':return b''
                if args[0]=='rev-parse':return b'a'*40
                if args[0]=='archive':return buf.getvalue()
                if args[0]=='show' and args[1].endswith('server/accounts/context.py'):return b"data={'release_version':'6.33.0'}"
                if args[0]=='show':return b'__VERSION__ __ARCHIVE_SHA256__'
                raise AssertionError(args)
            with patch.object(module,'git',side_effect=git),patch.object(sys,'argv',['package','--version','6.33.0','--output',str(output)]):module.main()
            with zipfile.ZipFile(output/'openIA-6.33.0-update.zip') as z:
                names={Path(n).relative_to('openIA-feature-accounts-foundation').as_posix() for n in z.namelist()}
            self.assertEqual(names,{'README.md','RELEASE_6_33.md','THIRD_PARTY_NOTICES.md','server/config/settings.py','deploy/synology/wheels/test.whl'})
            meta=json.loads((output/'openIA-6.33.0-update.json').read_text());self.assertFalse(meta['ready_for_install']);self.assertFalse(meta['published'])

if __name__=='__main__':unittest.main()
