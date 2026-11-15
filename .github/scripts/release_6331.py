"""Trusted release policy. Never import or execute code from the candidate while signing."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

VERSION = '6.33.1'
SOURCE = 'a31400fad1fa63476f978a042cf046a83c600daa'
CANDIDATE = 'b043f3b5f9fe8eade56d1492a03ed76747338c7e'
ARCHIVE = 'openIA-6.33.1-update.zip'
INSTALLER = 'install-6.33.1.sh'
METADATA = 'openIA-6.33.1-update.json'
SUMS = 'SHA256SUMS-6.33.1'
PUBLIC = 'verification-public.pem'
ARCHIVE_SHA = 'dbc5145d24e0c8cdbeac8430e46af1afd56e63dc3b0b5e4059cb81fe12c795b2'
INSTALLER_SHA = 'a213b425e06dc753b2e548bca516080e4b3df686c874db70c11669cdf8bca1e8'
TRUSTED_KEY_SHA = 'e2f6b26419ccb10faa61baf1cfb5a0ea53f499da8803f23f0dd0df3f420aa3e4'
FILES = (ARCHIVE, INSTALLER, METADATA)
PUBLISHED_FILES = FILES + (SUMS, SUMS + '.sig')


class Refusal(Exception):
    pass


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise Refusal('Expected a regular release file.')


def openssl(*args):
    result = subprocess.run(['openssl', *map(str, args)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise Refusal('OpenSSL operation failed. Private diagnostics are suppressed.')
    return result.stdout


def confirmation(confirmed, backup_sha256, verified_at):
    if confirmed is not True:
        raise Refusal('Owner must confirm successful restoration of a real backup.')
    if not re.fullmatch('[0-9a-f]{64}', backup_sha256) or backup_sha256 == '0' * 64:
        raise Refusal('A real backup SHA-256 is required; never upload the backup itself.')
    moment = datetime.fromisoformat(verified_at.replace('Z', '+00:00'))
    if moment.tzinfo is None or moment > datetime.now(timezone.utc):
        raise Refusal('Restoration timestamp must include a timezone and cannot be in the future.')
    return {'restored': True, 'backup_sha256': backup_sha256,
            'verified_at': moment.isoformat(), 'kind': 'owner_attestation_not_automatic_restore'}


def candidate(directory, signed=False):
    for name in FILES:
        regular(directory / name)
    data = json.loads((directory / METADATA).read_text())
    if (data.get('version') != VERSION or data.get('source_commit') != SOURCE
            or data.get('archive') != ARCHIVE or data.get('installer') != INSTALLER
            or data.get('archive_sha256') != ARCHIVE_SHA or data.get('installer_sha256') != INSTALLER_SHA
            or data.get('ready_for_install') is not signed or data.get('signature_created') is not signed
            or digest(directory / ARCHIVE) != ARCHIVE_SHA or digest(directory / INSTALLER) != INSTALLER_SHA):
        raise Refusal('Artifacts differ from the independently verified 6.33.1 candidate.')
    return data


def check_source(directory, source):
    candidate(directory)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args])
    if git('rev-parse', 'HEAD').decode().strip() != SOURCE:
        raise Refusal('Unexpected source revision.')
    def excluded(name):
        p = Path(name)
        return (p.parts[0] in ('previous-draft', 'tests')
                or p.parts[:3] == ('deploy', 'hotfixes', 'historical')
                or p.name == 'AGENTS.md'
                or len(p.parts) == 1 and (p.suffix in ('.zip', '.sig', '.bundle', '.pem', '.key')
                    or p.suffix == '.md' and p.name not in ('README.md', 'THIRD_PARTY_NOTICES.md', 'RELEASE_6_33.md', 'RELEASE_6_33_1.md')))
    generated = {'deploy/beget/release-files.txt', 'deploy/beget/known-release-paths.txt'}
    prefix = 'openIA-feature-accounts-foundation/'
    with zipfile.ZipFile(io.BytesIO(git('archive', '--format=zip', 'HEAD'))) as src, zipfile.ZipFile(directory / ARCHIVE) as dst:
        expected = {i.filename for i in src.infolist() if not i.is_dir() and not excluded(i.filename)}
        actual = {i.filename[len(prefix):] for i in dst.infolist() if not i.is_dir()}
        if len(dst.namelist()) != len(set(dst.namelist())) or any(not n.startswith(prefix) for n in dst.namelist()):
            raise Refusal('Invalid release archive layout.')
        if actual != expected | generated:
            raise Refusal('Unexpected release archive contents.')
        for name in actual - generated:
            if dst.read(prefix + name) != src.read(name):
                raise Refusal('Archive bytes differ from reviewed source.')
        if set(dst.read(prefix + 'deploy/beget/release-files.txt').decode().splitlines()) != actual:
            raise Refusal('Invalid generated file list.')
        if dst.read(prefix + 'deploy/beget/known-release-paths.txt').strip():
            raise Refusal('Unexpected historical file list for this candidate.')
    print('Reviewed ZIP matches the immutable source commit.')


def sign(directory, output, key_text, proof):
    data = candidate(directory)
    # Revalidate attestation even if this function is called independently of the CLI.
    proof = confirmation(proof.get('restored'), proof.get('backup_sha256', ''), proof.get('verified_at', ''))
    if not key_text:
        raise Refusal('Configure ARMADA_RELEASE_SIGNING_KEY in the armada-release GitHub Environment.')
    if output.exists():
        raise Refusal('Refusing to overwrite signing output.')
    with tempfile.TemporaryDirectory(prefix='armada-release-key-') as temp:
        key = Path(temp) / 'private.pem'
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(key_text)
        public_der = openssl('pkey', '-in', key, '-pubout', '-outform', 'DER')
        fingerprint = hashlib.sha256(public_der).hexdigest()
        if fingerprint != TRUSTED_KEY_SHA:
            raise Refusal('Signing identity differs from the already trusted 6.33.0 key.')
        public_pem = openssl('pkey', '-in', key, '-pubout')
        output.mkdir(mode=0o700)
        try:
            for name in (ARCHIVE, INSTALLER):
                shutil.copy2(directory / name, output / name)
            data.update(ready_for_install=True, signature_created=True,
                        signing_public_key_sha256=fingerprint, owner_restore_confirmation=proof)
            (output / METADATA).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            manifest = output / SUMS
            manifest.write_text(''.join(digest(output / name) + '  ' + name + '\n' for name in FILES))
            openssl('dgst', '-sha256', '-sign', key, '-out', output / (SUMS + '.sig'), manifest)
            (output / PUBLIC).write_bytes(public_pem)
            check_signed(output)
        except BaseException:
            shutil.rmtree(output)
            raise
    print('Signed the fixed candidate; private key is not included in output.')


def check_signed(directory):
    data = candidate(directory, signed=True)
    proof = data.get('owner_restore_confirmation', {})
    confirmation(proof.get('restored'), proof.get('backup_sha256', ''), proof.get('verified_at', ''))
    for name in (SUMS, SUMS + '.sig', PUBLIC):
        regular(directory / name)
    public = directory / PUBLIC
    fp = hashlib.sha256(openssl('pkey', '-pubin', '-in', public, '-outform', 'DER')).hexdigest()
    if fp != TRUSTED_KEY_SHA or data.get('signing_public_key_sha256') != fp:
        raise Refusal('Unexpected public verification key.')
    openssl('dgst', '-sha256', '-verify', public, '-signature', directory / (SUMS + '.sig'), directory / SUMS)
    hashes = {}
    for line in (directory / SUMS).read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in hashes:
            raise Refusal('Invalid signed checksum manifest.')
        hashes[match[2]] = match[1]
    if set(hashes) != set(FILES) or any(digest(directory / name) != hashes[name] for name in FILES):
        raise Refusal('Signed artifact mismatch.')
    print('Independent signature and all three signed checksums verified.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('check', 'sign', 'check-signed', 'attestation'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.command in ('attestation', 'sign'):
            proof = confirmation(os.environ.get('RESTORE_CONFIRMED') == 'true',
                                 os.environ.get('RESTORE_BACKUP_SHA256', ''), os.environ.get('RESTORE_VERIFIED_AT', ''))
        if args.command == 'attestation':
            print('Owner restoration attestation is complete; this is not automatic backup verification.')
        elif args.command == 'check':
            candidate(args.directory)
            if args.source_root:
                check_source(args.directory, args.source_root)
        elif args.command == 'sign':
            sign(args.directory, args.output, os.environ.get('ARMADA_RELEASE_SIGNING_KEY', ''), proof)
        else:
            check_signed(args.directory)
    except Exception:
        # Never print exception messages, arguments, environment, OpenSSL stderr or key data.
        print('Release step refused: verify candidate hashes, owner restoration attestation, and trusted signing key. Private diagnostics are suppressed.', file=__import__('sys').stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
