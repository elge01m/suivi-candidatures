"""Interactive local setup. Never run with redirected input or a transcript."""

import getpass
import json
import os
from pathlib import Path
import sys
import traceback

import psycopg
from psycopg import sql
from dotenv import set_key


BACKEND = Path(__file__).resolve().parents[1]
RESULT = BACKEND / '.venv' / 'postgresql-setup-result.json'
DATABASE = 'suivi_candidatures'
ROLE = 'suivi_candidatures_app'


def record(state, **details):
    RESULT.write_text(json.dumps({'state': state, **details}), encoding='utf-8')


def main():
    record('waiting_for_local_input')
    if not sys.stdin.isatty():
        raise RuntimeError('interactive_terminal_required')
    if (BACKEND / '.env').exists():
        raise RuntimeError('existing_env_preserved')
    print('Configuration locale PostgreSQL. Aucune migration ne sera executee.')
    print('Les mots de passe sont saisis sans echo, uniquement dans ce terminal.')
    admin_password = getpass.getpass('Mot de passe PostgreSQL du role postgres : ')
    with psycopg.connect(
        host='127.0.0.1', port=5432, dbname='postgres', user='postgres',
        password=admin_password, connect_timeout=5, autocommit=True,
    ) as admin:
        del admin_password
        record('admin_connected')
        role_exists = admin.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)', (ROLE,)
        ).fetchone()[0]
        database_exists = admin.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)', (DATABASE,)
        ).fetchone()[0]
        if role_exists or database_exists:
            record('existing_objects_preserved', role_exists=role_exists,
                   database_exists=database_exists)
            print('Un nom existe deja. Aucun objet existant ne sera modifie.')
            return
        print('Verification terminee : le role et la base sont absents.')
        record('names_available')
        password = getpass.getpass('Nouveau mot de passe du role applicatif : ')
        if not password or password != getpass.getpass('Confirmer le mot de passe applicatif : '):
            raise RuntimeError('password_empty_or_mismatch')
        # Encrypt locally before sending SQL, as psql's \password does.
        verifier = admin.pgconn.encrypt_password(
            password.encode('utf-8'), ROLE.encode('utf-8'), b'scram-sha-256'
        ).decode('ascii')
        record('password_verifier_ready')
        admin.execute(sql.SQL(
            'CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOREPLICATION NOBYPASSRLS PASSWORD {}'
        ).format(sql.Identifier(ROLE), sql.Literal(verifier)))
        record('role_created')
        admin.execute(sql.SQL(
            "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0"
        ).format(sql.Identifier(DATABASE), sql.Identifier(ROLE)))
        record('database_created')
        admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(
            sql.Identifier(DATABASE)
        ))
        with psycopg.connect(
            host='127.0.0.1', port=5432, dbname=DATABASE, user='postgres',
            # Reuse the authenticated connection password in memory only.
            password=admin.info.password, connect_timeout=5, autocommit=True,
        ) as database_admin:
            database_admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            database_admin.execute(sql.SQL('GRANT USAGE, CREATE ON SCHEMA public TO {}').format(
                sql.Identifier(ROLE)
            ))
    values = {
        'DB_NAME': DATABASE, 'DB_USER': ROLE, 'DB_PASSWORD': password,
        'DB_HOST': '127.0.0.1', 'DB_PORT': '5432',
    }
    env_path = BACKEND / '.env'
    # Exclusive creation: preserve any file created concurrently.
    with env_path.open('x', encoding='utf-8'):
        pass
    for key, value in values.items():
        set_key(str(env_path), key, value, quote_mode='always')
        os.environ[key] = value
    record('env_created')
    sys.path.insert(0, str(BACKEND))
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
    import django
    django.setup()
    from django.core.checks import run_checks
    from django.db import connection
    issues = run_checks()
    if any(issue.level >= 40 for issue in issues):
        raise RuntimeError('django_system_check_failed')
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_database(), current_user, version()')
        database_name, user_name, version = cursor.fetchone()
        cursor.execute(
            'SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls '
            'FROM pg_roles WHERE rolname = current_user'
        )
        if any(cursor.fetchone()):
            raise RuntimeError('unexpected_role_privileges')
        cursor.execute(
            "SELECT has_schema_privilege(current_user, 'public', 'USAGE'), "
            "has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        if not all(cursor.fetchone()):
            raise RuntimeError('missing_schema_privileges')
        cursor.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        table_count = cursor.fetchone()[0]
    connection.close()
    record('success', database=database_name, role=user_name,
           server_version=version, public_table_count=table_count,
           django_checks='passed', elevated_privileges=False)
    print('OK : Django se connecte a PostgreSQL avec le role applicatif.')
    print('Permissions verifiees. Aucune migration executee.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # Never print exception text, connection strings or local variables.
        previous = json.loads(RESULT.read_text()) if RESULT.exists() else {}
        record('failed', last_state=previous.get('state'), error_type=type(error).__name__,
               sqlstate=getattr(error, 'sqlstate', None),
               frames=[{'file': Path(frame.filename).name, 'line': frame.lineno,
                        'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)])
        print('Configuration interrompue. Aucun detail sensible ne sera affiche.')
        sys.exit(1)
