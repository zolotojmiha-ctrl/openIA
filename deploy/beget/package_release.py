"""Package an immutable, clean source commit. Does not publish or run installers."""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=root)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        raise SystemExit('Invalid release version')
    if git('status', '--porcelain').strip():
        raise SystemExit('Commit the reviewed sources before packaging')
    head = git('rev-parse', 'HEAD').decode().strip()
    tree = ast.parse(git('show', head + ':server/accounts/context.py').decode())
    versions = [v.value for node in ast.walk(tree) if isinstance(node, ast.Dict) for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and k.value == 'release_version' and isinstance(v, ast.Constant)]
    if versions != [args.version]:
        raise SystemExit('Source version differs from requested release')
    output = args.output.resolve()
    if output.is_relative_to(root):
        raise SystemExit('Keep release artifacts outside the source checkout')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'openIA-{args.version}-update.zip'
    installer = output / f'install-{args.version}.sh'
    metadata = output / f'openIA-{args.version}-update.json'
    if any(p.exists() for p in (archive, installer, metadata)):
        raise SystemExit('Refusing to overwrite existing release artifacts')
    # Installable code only; the full immutable source remains in Git/bundle.
    import io
    raw=git('archive','--format=zip','--prefix=openIA-feature-accounts-foundation/',head)
    excluded=[]
    with zipfile.ZipFile(io.BytesIO(raw)) as src,zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as dst:
        for item in src.infolist():
            relative=Path(item.filename).relative_to('openIA-feature-accounts-foundation')
            if relative.parts and (relative.parts[0] in ('previous-draft','tests') or len(relative.parts)==1 and relative.suffix in ('.zip','.sig','.bundle','.pem','.key') or relative.name=='AGENTS.md' or len(relative.parts)==1 and relative.suffix=='.md' and relative.name not in ('README.md','THIRD_PARTY_NOTICES.md',f'RELEASE_{"_".join(args.version.split(".")[:2])}.md')):
                excluded.append(str(relative));continue
            dst.writestr(item,src.read(item.filename))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name in z.namelist():
            assert not any(p in ('.git', '.runtime', '.env', '__pycache__') for p in Path(name).parts)
        files = len([i for i in z.infolist() if not i.is_dir()])
    template = git('show', head + ':deploy/beget/install-update.sh.in').decode()
    installer.write_text(template.replace('__VERSION__', args.version).replace('__ARCHIVE_SHA256__', sha(archive)))
    installer.chmod(0o700)
    data = {'version': args.version, 'source_commit': head, 'archive': archive.name, 'archive_sha256': sha(archive),
            'archive_files': files,'excluded_service_entries':len(excluded), 'installer': installer.name, 'installer_sha256': sha(installer),
            'status': 'candidate', 'ready_for_install': False, 'published': False,
            'production_installed': False, 'max_default_enabled': False, 'live_max_checked': False,'signature_required':True,'signature_created':False,'checksums':f'SHA256SUMS-{args.version}','signature':f'SHA256SUMS-{args.version}.sig'}
    metadata.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(sha(p) + '  ' + p.name + '\n' for p in (archive, installer)))
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
