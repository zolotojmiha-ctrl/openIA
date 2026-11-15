"""Owner terminal helper: restore a private backup locally, sign public release files. No uploads."""
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


# Public artifacts only. Inputs containing real data and private keys never leave
# the owner's computer or enter a Docker build context / response archive.
BASE_URL = 'https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/' + CANDIDATE
POSTGRES_IMAGE = 'postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826'
OUTER = 'armada-6.33.1.zip'
PREFIX = 'armada-6.33.1/'
OUTER_FILES = FILES + (SUMS, SUMS + '.sig', 'armada-release-signing.public.pem', 'backup-recovery.public.pem')
RESPONSE_FILES = (METADATA, SUMS, SUMS + '.sig', PUBLIC, 'backup-recovery.public.pem', OUTER + '.sig', OUTER + '.sha256')


def choose_file(prompt):
    result = subprocess.run(['osascript', '-e', 'POSIX path of (choose file with prompt ' + json.dumps(prompt, ensure_ascii=False) + ')'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise Refusal('Выбор отменён. Установка и подписание не выполнялись.')
    path = Path(result.stdout.strip())
    regular(path)
    return path.resolve()


def download(name, target):
    # Fixed repository/immutable commit; never an input URL or upload operation.
    subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--proto', '=https',
                    '--output', str(target), BASE_URL + '/' + name], check=True)


def extract_code(archive, output):
    import stat
    with zipfile.ZipFile(archive) as z:
        seen = set()
        for info in z.infolist():
            p = Path(info.filename)
            if (not info.filename.startswith('openIA-feature-accounts-foundation/') or p.is_absolute()
                    or '..' in p.parts or '\\' in info.filename or info.filename in seen
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise Refusal('Небезопасный состав пакета.')
            seen.add(info.filename)
        z.extractall(output)
    return output / 'openIA-feature-accounts-foundation'


def outer_package(signed, target):
    """Stored entries with fixed metadata: cloud can reproduce identical bytes."""
    check_signed(signed)
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_STORED) as z:
        for name in OUTER_FILES:
            path = signed / (PUBLIC if name == 'armada-release-signing.public.pem' else name)
            info = zipfile.ZipInfo(PREFIX + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, path.read_bytes())
    return digest(target)


def public_response(signed, outer, key, target):
    # key is a temporary LOCAL key file, outside every build/output directory.
    openssl('dgst', '-sha256', '-sign', key, '-out', signed / (OUTER + '.sig'), outer)
    openssl('dgst', '-sha256', '-verify', signed / PUBLIC,
            '-signature', signed / (OUTER + '.sig'), outer)
    (signed / (OUTER + '.sha256')).write_text(digest(outer) + '  ' + OUTER + '\n')
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_STORED) as z:
        for name in RESPONSE_FILES:
            z.write(signed / name, name)
    if target.stat().st_size > 1048576:
        target.unlink()
        raise Refusal('Неожиданный размер публичного ответа; передача запрещена.')
    with zipfile.ZipFile(target) as z:
        if set(z.namelist()) != set(RESPONSE_FILES) or any(b'PRIVATE KEY' in z.read(n) for n in z.namelist()):
            target.unlink()
            raise Refusal('В публичный ответ попали недопустимые данные.')


def desktop():
    docker = Path('/Applications/Docker.app/Contents/Resources/bin/docker')
    if not docker.is_file():
        raise Refusal('Не найден Docker Desktop. Он нужен только для изолированного восстановления копии.')
    env = os.environ.copy()
    for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(name, None)
    cli = [str(docker), '--context', 'desktop-linux']
    endpoint = subprocess.check_output(cli + ['context', 'inspect', 'desktop-linux', '--format', '{{.Endpoints.docker.Host}}'], env=env, text=True).strip()
    system = subprocess.check_output(cli + ['info', '--format', '{{.OperatingSystem}}'], env=env, text=True).strip()
    if not endpoint.startswith('unix://') or 'Docker Desktop' not in system:
        raise Refusal('Требуется локальный Docker Desktop. Удалённый сервер не используется.')
    memory = subprocess.check_output(cli + ['info', '--format', '{{.MemTotal}}'], env=env, text=True).strip()
    if not memory.isdigit() or int(memory) < 1536 * 1048576:
        raise Refusal('Для ограниченного профиля восстановления требуется минимум 1536 МиБ памяти Docker Desktop.')
    return cli, env


def restore(cli, env, code, archive, recovery_key, receipt_dir, expected_sha):
    # Reuse the reviewed recovery Dockerfile and verifier, including pinned images,
    # offline hash-locked wheels, authenticated decryption, file and row checks.
    for path in (archive, recovery_key):
        if path.is_relative_to(code.resolve()) or ',' in str(path):
            raise Refusal('Архив и ключ восстановления должны быть вне каталога кода; пути без запятых.')
    if archive.suffix != '.encrypted' or archive.stat().st_size > 512 * 1048576:
        raise Refusal('Нужна копия .encrypted размером до 512 МиБ для ограниченного профиля восстановления. Большую копию нельзя проверять с этими лимитами.')
    image = 'armada-owner-recovery:6331'
    subprocess.run(cli + ['build', '--platform', 'linux/amd64', '--network', 'none', '-t', image,
                         '-f', str(code / 'deploy/recovery/Dockerfile'), str(code)], env=env, check=True)
    subprocess.run(cli + ['pull', '--platform', 'linux/amd64', POSTGRES_IMAGE], env=env, check=True)
    receipt = receipt_dir / 'restored-real-backup.json'
    if receipt.exists():
        raise Refusal('Квитанция уже существует: требуется новый результат текущего запуска.')
    inside_archive = '/inputs/' + archive.name
    subprocess.run(cli + ['run', '--rm', '--pull', 'never', '--platform', 'linux/amd64',
        '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE',
        '--security-opt', 'no-new-privileges:true', '--memory', '512m',
        '--tmpfs', '/tmp:rw,size=2g,mode=1777', '--env', 'DOCKER_DEFAULT_PLATFORM=linux/amd64',
        '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly',
        '--mount', 'type=bind,src=' + str(archive) + ',dst=' + inside_archive + ',readonly',
        '--mount', 'type=bind,src=' + str(recovery_key) + ',dst=/inputs/recovery.pem,readonly',
        '--mount', 'type=bind,src=' + str(receipt_dir) + ',dst=/receipts',
        image, '--archive', inside_archive, '--private-key', '/inputs/recovery.pem',
        '--receipt', '/receipts/' + receipt.name, '--postgres-image', POSTGRES_IMAGE], env=env, check=True)
    regular(receipt)
    proof = json.loads(receipt.read_text())
    if (proof.get('restored') is not True or proof.get('sha256') != expected_sha
            or digest(archive) != expected_sha or type(proof.get('tables')) is not int or proof['tables'] < 1
            or type(proof.get('verified_at')) is not int):
        raise Refusal('Успешное восстановление выбранной реальной копии не подтверждено.')
    return proof


def owner_main():
    if __import__('platform').system() != 'Darwin':
        print('Этот шаг выполняется владельцем на доверенном Mac, где находятся ключи и реальная копия.', file=__import__('sys').stderr)
        return 1
    phase = 'проверка локального Docker'
    try:
        cli, env = desktop()
        print('Выберите три существующих файла. Их содержимое никуда не отправляется.')
        archive = choose_file('Выберите РЕАЛЬНУЮ резервную копию из ЛК (.encrypted)')
        recovery_key = choose_file('Выберите КЛЮЧ ВОССТАНОВЛЕНИЯ этой копии — не SSH и не ключ подписи')
        signing_key = choose_file('Выберите armada-release-signing.key.pem — прежний ключ подписи выпусков')
        backup_sha = digest(archive)
        downloads = Path.home() / 'Downloads'
        downloads.mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix='armada-6.33.1-owner-', dir=downloads))
        phase = 'скачивание проверенного кандидата'
        with tempfile.TemporaryDirectory(prefix='armada-owner-work-') as td:
            work = Path(td)
            unsigned = work / 'unsigned'
            unsigned.mkdir(mode=0o700)
            for name in FILES:
                download(name, unsigned / name)
            candidate(unsigned)
            code = extract_code(unsigned / ARCHIVE, work / 'code')
            phase = 'восстановление реальной копии'
            print('Проверяем копию во временной базе без сети. Рабочий VPS не используется.')
            receipt_dir = output / 'local-receipt'
            receipt_dir.mkdir(mode=0o700)
            proof = restore(cli, env, code, archive, recovery_key, receipt_dir, backup_sha)
            phase = 'подписание'
            print('Восстановление подтверждено. Подписываем выпуск прежним ключом.')
            # OpenSSL may request the key's passphrase on the LOCAL terminal.
            # Private stdout is captured in memory, never printed or uploaded.
            private = subprocess.run(['openssl', 'pkey', '-in', str(signing_key)],
                                     stdout=subprocess.PIPE, check=True).stdout
            key = work / 'private-signing.pem'
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(private)
            attestation = confirmation(True, backup_sha, datetime.fromtimestamp(proof['verified_at'], timezone.utc).isoformat())
            signed = work / 'signed'
            sign(unsigned, signed, private.decode(), attestation)
            private = b''
            (signed / 'backup-recovery.public.pem').write_bytes(openssl('pkey', '-in', recovery_key, '-pubout'))
            outer = output / OUTER
            outer_package(signed, outer)
            response = output / 'armada-6.33.1-public-signatures.zip'
            public_response(signed, outer, key, response)
            key.unlink()
        print('\nГОТОВО: реальная копия восстановлена, пакет подписан и проверен.')
        print('Для публикации на GitHub прикрепите в чат ТОЛЬКО этот небольшой публичный файл:')
        print(response)
        print('В нём нет закрытых ключей, базы, документов или квитанции с частными именами.')
        print('Не отправляйте исходные ключи, резервную копию или папку local-receipt.')
        return 0
    except Refusal as error:
        print('Остановлено: ' + str(error), file=__import__('sys').stderr)
    except subprocess.CalledProcessError as error:
        print('Этап «' + phase + '» завершился с кодом ' + str(error.returncode) + '. Готовность к установке не подтверждена.', file=__import__('sys').stderr)
    except Exception:
        print('Этап «' + phase + '» не завершён. Подпись/готовность не подтверждены. Не передавайте ключи или данные.', file=__import__('sys').stderr)
    return 1

if __name__ == '__main__':
    raise SystemExit(owner_main())
