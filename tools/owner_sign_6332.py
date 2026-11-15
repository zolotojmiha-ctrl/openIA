"""Owner-local signing only. No Docker, restore, VPS access, uploads or GitHub secrets."""
import argparse
from datetime import datetime,timezone
import hashlib,json,os,re,shutil,subprocess,tempfile,zipfile
from pathlib import Path

VERSION='6.33.2'
TRUSTED_KEY_SHA='e2f6b26419ccb10faa61baf1cfb5a0ea53f499da8803f23f0dd0df3f420aa3e4'
BACKUP_PUBLIC_SHA='ed88e924d64fa785d343907855b0245b2a01c932cfdb28a44ff3490e77aa31a4'
ARCHIVE=f'openIA-{VERSION}-update.zip'
INSTALLER=f'install-{VERSION}.sh'
META=f'openIA-{VERSION}-update.json'
SUMS=f'SHA256SUMS-{VERSION}'
OUTER=f'armada-{VERSION}.zip'
PUBLIC='armada-release-signing.public.pem'
BACKUP='backup-recovery.public.pem'
FILES=(ARCHIVE,INSTALLER,META)
OUTER_FILES=FILES+(SUMS,SUMS+'.sig',PUBLIC,BACKUP)
RESPONSE_FILES=(META,SUMS,SUMS+'.sig',PUBLIC,BACKUP,OUTER+'.sig',OUTER+'.sha256')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def openssl(*args):
    return subprocess.run(['openssl',*map(str,args)],check=True,stdout=subprocess.PIPE).stdout


def fingerprint(path):return hashlib.sha256(openssl('pkey','-pubin','-in',path,'-outform','DER')).hexdigest()


def prior_confirmation(baseline):
    if fingerprint(baseline/PUBLIC)!=TRUSTED_KEY_SHA:raise ValueError('Прежний доверенный ключ не совпал.')
    manifest=baseline/'SHA256SUMS-6.33.1'
    openssl('dgst','-sha256','-verify',baseline/PUBLIC,'-signature',baseline/'SHA256SUMS-6.33.1.sig',manifest)
    hashes=dict(line.split('  ',1)[::-1] for line in manifest.read_text().splitlines())
    metadata=baseline/'openIA-6.33.1-update.json'
    if hashes[metadata.name]!=sha(metadata):raise ValueError('Изменено прежнее подписанное подтверждение.')
    data=json.loads(metadata.read_text());proof=data.get('owner_restore_confirmation',{})
    if (data.get('version')!='6.33.1' or proof.get('restored') is not True or
        not re.fullmatch('[0-9a-f]{64}',proof.get('backup_sha256','')) or not proof.get('verified_at')):
        raise ValueError('Нет прежнего подписанного подтверждения восстановления.')
    if sha(baseline/BACKUP)!=BACKUP_PUBLIC_SHA:raise ValueError('Изменён публичный ключ резервирования.')
    return proof,sha(metadata)


def outer_package(directory,target):
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_STORED) as z:
        for name in OUTER_FILES:
            info=zipfile.ZipInfo(f'armada-{VERSION}/'+name,date_time=(1980,1,1,0,0,0))
            info.create_system=3;info.external_attr=0o100644<<16;info.compress_type=zipfile.ZIP_STORED
            z.writestr(info,(directory/name).read_bytes())


def verify(directory,archive_sha,installer_sha,source):
    if sha(directory/ARCHIVE)!=archive_sha or sha(directory/INSTALLER)!=installer_sha:
        raise ValueError('Контрольные суммы кандидата не совпали.')
    data=json.loads((directory/META).read_text())
    if data.get('version')!=VERSION or data.get('source_commit')!=source:
        raise ValueError('Версия или коммит кандидата не совпали.')
    if data.get('signature_created') or data.get('ready_for_install'):
        raise ValueError('Ожидался неподписанный кандидат.')
    return data


def _sign(directory,output,key_file,baseline,archive_sha,installer_sha,source,ownership):
    if output.exists():raise ValueError('Каталог подписи уже существует; повторная запись запрещена.')
    data=verify(directory,archive_sha,installer_sha,source)
    proof,prior_sha=prior_confirmation(baseline)
    with tempfile.TemporaryDirectory(prefix='armada-owner-key-') as td:
        key=Path(td)/'private.pem'
        # OpenSSL alone asks for the encrypted key passphrase on the owner's terminal.
        private=openssl('pkey','-in',key_file)
        fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(private)
        private=b''
        public=openssl('pkey','-in',key,'-pubout')
        public_path=Path(td)/'public.pem';public_path.write_bytes(public)
        if fingerprint(public_path)!=TRUSTED_KEY_SHA:raise ValueError('Выбран другой ключ подписи.')
        output.mkdir(mode=0o700)
        ownership['created']=True
        for name in (ARCHIVE,INSTALLER):shutil.copy2(directory/name,output/name)
        data.update(ready_for_install=True,signature_created=True,signing_public_key_sha256=TRUSTED_KEY_SHA,
            owner_restore_confirmation=proof,restore_validation_basis={'release':'6.33.1','signed_metadata_sha256':prior_sha,
            'note':'Prior real restore validation reused; no new real restore claimed. Installer makes a fresh production checkpoint.'},
            signed_at=datetime.now(timezone.utc).isoformat())
        (output/META).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        (output/PUBLIC).write_bytes(public);shutil.copy2(baseline/BACKUP,output/BACKUP)
        (output/SUMS).write_text(''.join(sha(output/name)+'  '+name+'\n' for name in FILES))
        openssl('dgst','-sha256','-sign',key,'-out',output/(SUMS+'.sig'),output/SUMS)
        openssl('dgst','-sha256','-verify',output/PUBLIC,'-signature',output/(SUMS+'.sig'),output/SUMS)
        outer_package(output,output/OUTER)
        openssl('dgst','-sha256','-sign',key,'-out',output/(OUTER+'.sig'),output/OUTER)
        openssl('dgst','-sha256','-verify',output/PUBLIC,'-signature',output/(OUTER+'.sig'),output/OUTER)
        (output/(OUTER+'.sha256')).write_text(sha(output/OUTER)+'  '+OUTER+'\n')
        response=output/f'armada-{VERSION}-public-signatures.zip'
        with zipfile.ZipFile(response,'x',compression=zipfile.ZIP_STORED) as z:
            for name in RESPONSE_FILES:
                blob=(output/name).read_bytes()
                if b'PRIVATE KEY' in blob:raise ValueError('Недопустимые данные в публичном ответе.')
                z.writestr(name,blob)
        if response.stat().st_size>1024*1024:raise ValueError('Неожиданный размер публичного ответа.')
    return response


def sign(directory,output,key_file,baseline,archive_sha,installer_sha,source):
    if output.exists():raise ValueError('Каталог подписи уже существует; повторная запись запрещена.')
    ownership={}
    try:return _sign(directory,output,key_file,baseline,archive_sha,installer_sha,source,ownership)
    except BaseException:
        if ownership.get('created'):shutil.rmtree(output)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--archive-sha',required=True)
    parser.add_argument('--installer-sha',required=True)
    parser.add_argument('--source',required=True)
    parser.add_argument('--key-file',type=Path)
    args=parser.parse_args()
    if not all(re.fullmatch('[0-9a-f]{64}',v) for v in (args.archive_sha,args.installer_sha)) or not re.fullmatch('[0-9a-f]{40}',args.source):raise ValueError('Неверный формат отпечатков.')
    key=args.key_file
    if not key:
        chosen=subprocess.run(['osascript','-e','POSIX path of (choose file with prompt "Выберите прежний armada-release-signing.key.pem")'],check=True,stdout=subprocess.PIPE,text=True)
        key=Path(chosen.stdout.strip())
    print('Проверяем кандидат и прежнее подписанное подтверждение восстановления. Новое восстановление не запускается.')
    response=sign(args.directory,args.directory/'signed',key,Path(__file__).parent/'release_6332_baseline',args.archive_sha,args.installer_sha,args.source)
    print('Пакет подписан и проверен. Для публикации передайте только публичный файл:')
    print(response)
    print('Закрытый ключ, пароли и данные платформы передавать не нужно.')


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError,KeyError) as exc:
        print('Подписание не завершено. Пакет не устанавливать. Ключи и данные не отправлять.')
        raise SystemExit(1)
