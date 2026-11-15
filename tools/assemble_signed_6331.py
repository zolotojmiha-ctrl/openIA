"""Reproduce and verify the owner's public signed package. No private key input."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
import owner_finish_6331 as release


def assemble(response, candidate_dir, output):
    if output.exists():
        raise release.Refusal('Refusing to overwrite assembly output.')
    if response.stat().st_size > 1048576:
        raise release.Refusal('Public response exceeds the expected size.')
    base = release.candidate(candidate_dir)
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix='armada-public-response-') as td:
            signed = Path(td)
            with zipfile.ZipFile(response) as z:
                if (len(z.namelist()) != len(set(z.namelist())) or set(z.namelist()) != set(release.RESPONSE_FILES)
                        or sum(i.file_size for i in z.infolist()) > 1048576):
                    raise release.Refusal('Unexpected public response contents.')
                for info in z.infolist():
                    if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                        raise release.Refusal('Links/directories are forbidden in the public response.')
                    data = z.read(info)
                    if b'PRIVATE KEY' in data:
                        raise release.Refusal('Private data is forbidden in the public response.')
                    (signed / info.filename).write_bytes(data)
            for name in (release.ARCHIVE, release.INSTALLER):
                shutil.copy2(candidate_dir / name, signed / name)
            release.check_signed(signed)
            metadata = json.loads((signed / release.METADATA).read_text())
            if set(metadata) != set(base) | {'signing_public_key_sha256', 'owner_restore_confirmation'}:
                raise release.Refusal('Unexpected signed metadata fields.')
            release.openssl('pkey', '-pubin', '-in', signed / 'backup-recovery.public.pem', '-outform', 'DER')
            outer = output / release.OUTER
            release.outer_package(signed, outer)
            expected = (signed / (release.OUTER + '.sha256')).read_text().strip()
            actual = release.digest(outer) + '  ' + release.OUTER
            if expected != actual:
                raise release.Refusal('Reconstructed outer ZIP differs from the owner-signed bytes.')
            release.openssl('dgst', '-sha256', '-verify', signed / release.PUBLIC,
                            '-signature', signed / (release.OUTER + '.sig'), outer)
            for name in (release.OUTER + '.sig', release.OUTER + '.sha256'):
                shutil.copy2(signed / name, output / name)
            details = output / 'fixes/release-6.33.1'
            details.mkdir(parents=True)
            for name in release.OUTER_FILES:
                source = signed / (release.PUBLIC if name == 'armada-release-signing.public.pem' else name)
                shutil.copy2(source, details / name)
            (output / 'VERIFIED.json').write_text(json.dumps({'version': release.VERSION, 'source_commit': release.SOURCE,
                'outer_sha256': release.digest(outer), 'trusted_der_sha256': release.TRUSTED_KEY_SHA,
                'inner_signature_verified': True, 'outer_signature_verified': True,
                'production_installed': False}, indent=2) + '\n')
    except BaseException:
        shutil.rmtree(output)
        raise
    return output


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--response', required=True, type=Path)
    p.add_argument('--candidate-dir', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    try:
        result = assemble(args.response, args.candidate_dir, args.output)
    except Exception:
        print('Public response verification failed; no release may be published.')
        return 1
    print('Verified public release ready for normal Git publication: ' + str(result))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
