"""Streaming hybrid encryption. The private recovery key never needs to be on the VPS."""
import hashlib,json,os,struct
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa,padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

MAGIC=b'ARMADA-BACKUP-1\n'
CHUNK=1024*1024

def generate():
    key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    private=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    return private,public.decode(),hashlib.sha256(public).hexdigest()

def digest(path):
    sha=hashlib.sha256();size=0
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(CHUNK),b''):sha.update(block);size+=len(block)
    return sha.hexdigest(),size

def encrypt(source,target,public):
    import base64
    key=os.urandom(32);nonce=os.urandom(12)
    public_key=serialization.load_pem_public_key(public.encode())
    if not isinstance(public_key,rsa.RSAPublicKey) or public_key.key_size<3072:raise ValueError('Invalid public recovery key')
    wrapped=public_key.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=MAGIC))
    header=json.dumps({'key':base64.b64encode(wrapped).decode(),'nonce':base64.b64encode(nonce).decode()},sort_keys=True).encode()
    prefix=MAGIC+struct.pack('>I',len(header))+header
    cipher=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor();cipher.authenticate_additional_data(prefix)
    created=False
    try:
        with open(source,'rb') as src,open(target,'xb',opener=lambda p,flags:os.open(p,flags,0o600)) as dst:
            created=True
            dst.write(prefix)
            for block in iter(lambda:src.read(CHUNK),b''):dst.write(cipher.update(block))
            dst.write(cipher.finalize());dst.write(cipher.tag);dst.flush();os.fsync(dst.fileno())
    except BaseException:
        if created:Path(target).unlink(missing_ok=True)
        raise
    return digest(target)

def decrypt(source,target,private):
    """Offline recovery. Publish output only after the GCM authentication tag is verified."""
    import base64
    key=serialization.load_pem_private_key(private,password=None)
    target=Path(target);pending=target.with_name(target.name+'.partial')
    if target.exists() or pending.exists():raise ValueError('Recovery output already exists')
    created=False
    try:
        with open(source,'rb') as src:
            if src.read(len(MAGIC))!=MAGIC:raise ValueError('Not an Armada backup')
            size_raw=src.read(4)
            if len(size_raw)!=4:raise ValueError('Truncated header')
            length=struct.unpack('>I',size_raw)[0]
            if not 1<=length<=16384:raise ValueError('Invalid header')
            header=src.read(length)
            if len(header)!=length:raise ValueError('Truncated header')
            data=json.loads(header)
            secret=key.decrypt(base64.b64decode(data['key'],validate=True),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=MAGIC))
            begin=src.tell();src.seek(0,2);end=src.tell()
            if end-begin<16:raise ValueError('Truncated ciphertext')
            src.seek(end-16);tag=src.read(16);src.seek(begin)
            cipher=Cipher(algorithms.AES(secret),modes.GCM(base64.b64decode(data['nonce'],validate=True),tag)).decryptor()
            cipher.authenticate_additional_data(MAGIC+size_raw+header)
            left=end-16-begin
            with open(pending,'xb',opener=lambda p,flags:os.open(p,flags,0o600)) as dst:
                created=True
                while left:
                    block=src.read(min(CHUNK,left))
                    if not block:raise ValueError('Truncated ciphertext')
                    left-=len(block);dst.write(cipher.update(block))
                dst.write(cipher.finalize());dst.flush();os.fsync(dst.fileno())
        # No overwrite, even if another process created the destination meanwhile.
        os.link(pending,target);pending.unlink()
    except BaseException:
        if created:pending.unlink(missing_ok=True)
        raise
    return digest(target)


def main():
    """Recovery without Django settings, a database, OAuth or a live VPS."""
    import argparse
    parser=argparse.ArgumentParser(description='Расшифровать резервную копию АРМАДА локально.')
    parser.add_argument('--source',required=True)
    keys=parser.add_mutually_exclusive_group(required=True)
    keys.add_argument('--private-key')
    keys.add_argument('--public-key')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.public_key:
            public=Path(args.public_key).read_text();encrypt(args.source,args.output,public)
            Path(args.output+'.key-id').write_text(hashlib.sha256(public.encode()).hexdigest())
        else:decrypt(args.source,args.output,Path(args.private_key).read_bytes())
    except Exception:
        parser.exit(1,'Архив не восстановлен. Проверьте файл копии, закрытый ключ и свободный путь результата.\n')
    print('Архив зашифрован.' if args.public_key else 'Целостность расшифрованного архива подтверждена.')

if __name__=='__main__':main()
