"""Reproduce the owner-signed outer archive from public response files only."""
import argparse,json,re,shutil,tempfile,zipfile
from pathlib import Path
import owner_sign_6332 as release


def assemble(candidate,response,output,source):
    if output.exists():raise ValueError('Output exists')
    with tempfile.TemporaryDirectory(prefix='armada-public-assembly-') as td:
        signed=Path(td)
        if response.stat().st_size>1024*1024:raise ValueError('Unexpected response size')
        with zipfile.ZipFile(response) as z:
            if len(z.namelist())!=len(release.RESPONSE_FILES) or set(z.namelist())!=set(release.RESPONSE_FILES):raise ValueError('Unexpected response members')
            if sum(info.file_size for info in z.infolist())>1024*1024 or any(info.compress_type!=zipfile.ZIP_STORED for info in z.infolist()):raise ValueError('Unexpected response encoding or expanded size')
            for name in z.namelist():
                blob=z.read(name)
                if b'PRIVATE KEY' in blob:raise ValueError('Private data in response')
                (signed/name).write_bytes(blob)
        if release.fingerprint(signed/release.PUBLIC)!=release.TRUSTED_KEY_SHA:raise ValueError('Untrusted key')
        if release.sha(signed/release.BACKUP)!=release.BACKUP_PUBLIC_SHA:raise ValueError('Unexpected backup public key')
        release.openssl('dgst','-sha256','-verify',signed/release.PUBLIC,'-signature',signed/(release.SUMS+'.sig'),signed/release.SUMS)
        manifest={}
        for line in (signed/release.SUMS).read_text().splitlines():
            match=re.fullmatch('([0-9a-f]{64})  ([A-Za-z0-9_.-]+)',line)
            if not match or match[2] in manifest:raise ValueError('Invalid manifest')
            manifest[match[2]]=match[1]
        if set(manifest)!=set(release.FILES):raise ValueError('Unexpected manifest files')
        for name in (release.ARCHIVE,release.INSTALLER):shutil.copy2(candidate/name,signed/name)
        if any(release.sha(signed/name)!=digest for name,digest in manifest.items()):raise ValueError('Signed checksum mismatch')
        data=json.loads((signed/release.META).read_text())
        if data.get('version')!=release.VERSION or data.get('source_commit')!=source or not data.get('ready_for_install') or not data.get('signature_created'):raise ValueError('Unexpected signed metadata')
        proof,_=release.prior_confirmation(Path(release.__file__).parent/'release_6332_baseline')
        if data.get('owner_restore_confirmation')!=proof:raise ValueError('Prior restore confirmation changed')
        release.outer_package(signed,signed/release.OUTER)
        expected=(signed/(release.OUTER+'.sha256')).read_text()
        if expected!=release.sha(signed/release.OUTER)+'  '+release.OUTER+'\n':raise ValueError('Outer archive differs from owner output')
        release.openssl('dgst','-sha256','-verify',signed/release.PUBLIC,'-signature',signed/(release.OUTER+'.sig'),signed/release.OUTER)
        output.mkdir()
        for name in (release.OUTER,release.OUTER+'.sig',release.OUTER+'.sha256'):shutil.copy2(signed/name,output/name)
        inner=output/'fixes'/f'release-{release.VERSION}';inner.mkdir(parents=True)
        for name in release.OUTER_FILES:shutil.copy2(signed/name,inner/name)
    return output/release.OUTER


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',required=True,type=Path);parser.add_argument('--response',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--source',required=True)
    args=parser.parse_args();print(assemble(args.candidate,args.response,args.output,args.source))


if __name__=='__main__':main()
