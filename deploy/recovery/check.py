#!/usr/bin/env python3
"""Run the unchanged verifier with phase/error categories, never private output."""
import argparse
from pathlib import Path
import sys
import traceback
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'beget'))
import verify_backup


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--private-key',type=Path)
    parser.add_argument('--postgres-image',default='postgres:17-bookworm')
    args=parser.parse_args(argv)
    original=verify_backup.subprocess.run
    state={'stage':'расшифровка и проверка файлов','last_error':None}
    def run(command,*a,**kw):
        words=list(command) if isinstance(command,(list,tuple)) else []
        stage=None
        if words and Path(str(words[0])).name=='docker':
            if 'pg_restore' in words:stage='восстановление PostgreSQL'
            elif 'pg_isready' in words:stage='ожидание PostgreSQL'
            elif 'psql' in words:stage='сверка количества записей'
            elif 'run' in words:stage='создание временного PostgreSQL'
        if stage and state['stage']!=stage:
            state['stage']=stage;state['last_error']=None;print('Этап: '+stage,flush=True)
        result=original(command,*a,**kw)
        if stage and result.returncode==0:state['last_error']=None
        if stage and result.returncode:
            # Only fixed categories, never stderr/SQL/paths/URL/arguments.
            raw=result.stderr or b''
            if isinstance(raw,bytes):raw=raw.decode(errors='replace')
            hint='команда завершилась с ошибкой'
            for needle,label in (
                ('permission denied','отказ в доступе'),
                ('cannot connect to the docker daemon','нет соединения с локальным Docker'),
                ('client version','несовместимость версии Docker API'),
                ('no space left','недостаточно места'),
                ('exec format error','несовместимость архитектуры'),
                ('rosetta','ошибка эмуляции Rosetta'),
                ('out of memory','нехватка памяти'),
                ('read-only file system','запись в файловую систему только для чтения'),
                ('no such image','не найден заранее скачанный образ'),
                ('image not found','не найден заранее скачанный образ')):
                if needle in raw.lower():hint=label;break
            state['last_error']=(result.returncode,hint)
        return result
    verify_backup.subprocess.run=run
    print('Этап: расшифровка и проверка файлов',flush=True)
    try:
        verify_backup.verify(args.archive,args.receipt,args.private_key,args.postgres_image)
    except Exception as error:
        print('Проверка остановлена. Этап: '+state['stage'])
        print('Тип ошибки: '+type(error).__name__)
        if state['last_error']:
            print('Код Docker: '+str(state['last_error'][0])+'; '+state['last_error'][1])
        allowed={'verify_backup.py','backup_manifest.py','disk_crypto.py'}
        frames=[f for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).name in allowed]
        for frame in frames[-4:]:
            print('Место: '+Path(frame.filename).name+':'+str(frame.lineno)+' ('+frame.name+')')
        print('Подтверждение восстановления не выдано. Содержимое данных и ключа не выводится.')
        return 1
    finally:
        verify_backup.subprocess.run=original
    print('Изолированное восстановление базы и файлов подтверждено.')
    return 0

if __name__=='__main__':raise SystemExit(main())
