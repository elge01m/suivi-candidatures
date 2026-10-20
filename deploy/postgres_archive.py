"""Encrypted PostgreSQL backup and non-destructive restore drill. No shell commands.

Linux operational tool; Windows supported ONLY against an explicitly isolated test
cluster. All failures are fixed messages, never raw PostgreSQL/key errors.
Fernet is bounded to 64 MiB plaintext archives to limit memory on a small server.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
from datetime import datetime, timezone

from cryptography.fernet import Fernet

MAX_PLAIN = 64 * 1024 * 1024
MAX_CIPHER = 90 * 1024 * 1024
REPOSITORY = Path(__file__).resolve().parents[1]


class ArchiveError(Exception):
    pass


def private_path(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or (directory and not stat.S_ISDIR(info.st_mode)) or (
            not directory and not stat.S_ISREG(info.st_mode)):
        raise ArchiveError('A private regular file/directory is required.')
    if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
        raise ArchiveError('Unsafe permissions or owner; use private owner-only paths.')
    return path.resolve()


def configuration(env):
    for key in ('PGHOST','PGPORT','PGUSER','PGDATABASE','PGPASSFILE','BACKUP_KEY_FILE','BACKUP_DIR'):
        if not env.get(key) or any(ord(c)<32 for c in env[key]):
            raise ArchiveError('Missing or invalid backup environment.')
    local=env['PGHOST'] in ('127.0.0.1','localhost','::1') or env['PGHOST'].startswith('/')
    if not local and (env.get('PGSSLMODE')!='verify-full' or not env.get('PGSSLROOTCERT')):
        raise ArchiveError('Remote backup connections require verified TLS and a CA.')
    if env.get('PGPASSWORD'):
        raise ArchiveError('Use a private PGPASSFILE, not PGPASSWORD.')
    if os.name != 'posix' and not (env.get('BACKUP_ISOLATED_TEST')=='1' and env['PGHOST']=='127.0.0.1'
            and env['PGPORT']!='5432' and env['PGUSER'].startswith('sync_test_')
            and env['PGDATABASE'].startswith(('suivi_sync_test_','test_suivi_sync_test_'))):
        raise ArchiveError('Operational backups require Linux; Windows is isolated-test only.')
    folder = private_path(env['BACKUP_DIR'],directory=True)
    key_file = private_path(env['BACKUP_KEY_FILE'])
    private_path(env['PGPASSFILE'])
    if folder.is_relative_to(REPOSITORY) or key_file.is_relative_to(folder):
        raise ArchiveError('Archives must be outside the repository; key must be stored separately.')
    raw = key_file.read_bytes().strip()
    if raw.decode('ascii') in (env.get('GOOGLE_TOKEN_ENCRYPTION_KEY'),env.get('GMAIL_MESSAGE_ENCRYPTION_KEY')):
        raise ArchiveError('Use an independent backup key.')
    cipher = Fernet(raw)
    child_env = dict(env)
    child_env.pop('PGPASSWORD',None)
    return folder, cipher, child_env


def checked(command, env, *, data=None):
    result = subprocess.run(command,input=data,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,timeout=120)
    if result.returncode:
        raise ArchiveError('PostgreSQL archive validation or restore failed.')


def decode_archive(path, folder, cipher):
    path = private_path(path)
    if path.parent != folder or not path.name.endswith('.fbackup') or path.stat().st_size > MAX_CIPHER:
        raise ArchiveError('Invalid archive location or size.')
    digest_path = private_path(str(path)+'.sha256')
    ciphertext=path.read_bytes()
    if digest_path.read_text(encoding='ascii').strip() != hashlib.sha256(ciphertext).hexdigest():
        raise ArchiveError('Archive checksum mismatch.')
    plain=cipher.decrypt(ciphertext)
    if len(plain)>MAX_PLAIN:
        raise ArchiveError('Archive exceeds the supported size.')
    return plain


def backup(folder, cipher, env, archive):
    archive=Path(archive).absolute()
    if archive.parent.resolve()!=folder or not archive.name.endswith('.fbackup') or archive.exists() or Path(str(archive)+'.sha256').exists():
        raise ArchiveError('Use a new archive filename in the private backup directory.')
    process=subprocess.Popen(['pg_dump','--format=custom','--no-owner','--no-acl','--no-password'],
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
    timer=threading.Timer(120,process.kill)
    timer.start()
    try:
        plain=process.stdout.read(MAX_PLAIN+1)
        if len(plain)>MAX_PLAIN:
            process.kill()
            raise ArchiveError('Backup exceeds 64 MiB; use a reviewed streaming backup procedure.')
        if process.wait(timeout=120)!=0:
            raise ArchiveError('PostgreSQL backup failed.')
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill();process.wait()
        process.stdout.close()
    checked(['pg_restore','--list','--no-password'],env,data=plain)
    ciphertext=cipher.encrypt(plain)
    fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as out:
        out.write(ciphertext);out.flush();os.fsync(out.fileno())
    fd=os.open(str(archive)+'.sha256',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='ascii') as out:
        out.write(hashlib.sha256(ciphertext).hexdigest()+'\n');out.flush();os.fsync(out.fileno())
    if decode_archive(archive,folder,cipher)!=plain:
        raise ArchiveError('Backup read-back validation failed.')


def restore_check(plain, env, target, owner=None):
    if not re.fullmatch(r'restore_check_[a-z0-9_]{1,40}',target) or target==env['PGDATABASE']:
        raise ArchiveError('Restore target must be a new restore_check_ database.')
    owner=owner or env.get('PGUSER','')
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',owner):
        raise ArchiveError('Invalid explicit restore owner.')
    import psycopg
    from psycopg import sql
    with psycopg.connect(dbname='postgres',host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],
            passfile=env['PGPASSFILE'],autocommit=True) as admin:
        if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s',(target,)).fetchone():
            raise ArchiveError('Restore target already exists; nothing will be overwritten.')
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(target),sql.Identifier(owner)))
    target_env=env | {'PGDATABASE':target}
    checked(['pg_restore','--exit-on-error','--single-transaction','--no-owner','--no-acl','--no-password',
             '--role',owner,'--dbname',target],target_env,data=plain)
    # A restored clone must never be processed before explicit operator review.
    with psycopg.connect(dbname=target,host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],passfile=env['PGPASSFILE']) as clone:
        if clone.execute("SELECT to_regclass('public.gmail_sync_syncconfiguration')").fetchone()[0]:
            clone.execute("UPDATE gmail_sync_syncconfiguration SET gmail_enabled=false,auto_analysis_enabled=false,blocked_code='restore_review'")
    # No DROP, --clean, migration, worker, provider request or overwrite exists here.


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['backup','verify','restore-check'])
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--target')
    parser.add_argument('--owner',help='Existing application role; operator must be allowed to SET ROLE to it.')
    args=parser.parse_args()
    try:
        if os.name=='posix':os.umask(0o077)
        folder,cipher,env=configuration(os.environ)
        if args.operation=='backup':
            backup(folder,cipher,env,args.archive)
        else:
            plain=decode_archive(args.archive,folder,cipher)
            checked(['pg_restore','--list','--no-password'],env,data=plain)
            if args.operation=='restore-check':restore_check(plain,env,args.target or '',args.owner)
        print('OK '+args.operation)
    except Exception:
        print('ERROR: archive operation refused or failed; no sensitive details logged.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
