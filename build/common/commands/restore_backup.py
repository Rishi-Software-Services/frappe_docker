import os
import datetime
import json
import tarfile
import hashlib
import frappe
import boto3

from new import get_password
from push_backup import DATE_FORMAT, check_environment_variables
from frappe.utils import get_sites, random_string
from frappe.installer import make_conf, get_conf_params, make_site_dirs, update_site_config
from check_connection import get_site_config, get_config, COMMON_SITE_CONFIG_FILE


def list_directories(path):
    directories = []
    for name in os.listdir(path):
        if os.path.isdir(os.path.join(path, name)):
            directories.append(name)
    return directories


def get_backup_dir():
    return os.path.join(
        os.path.expanduser('~'),
        'backups'
    )


def decompress_db(files_base, site):
    database_file = files_base + '-database.sql.gz'
    command = 'gunzip -c {database_file} > {database_extract}'.format(
        database_file=database_file,
        database_extract=database_file.replace('.gz', '')
    )

    print('Extract Database GZip for site {}'.format(site))
    os.system(command)


def restore_database(files_base, site_config_path, site):
    # restore database
    database_file = files_base + '-database.sql.gz'
    decompress_db(files_base, site)
    config = get_config()

    # Set db_type if it exists in backup site_config.json
    set_key_in_site_config('db_type', site, site_config_path)
    # Set db_host if it exists in backup site_config.json
    set_key_in_site_config('db_host', site, site_config_path)
    # Set db_port if it exists in backup site_config.json
    set_key_in_site_config('db_port', site, site_config_path)

    # get updated site_config
    site_config = get_site_config(site)

    # if no db_type exists, default to mariadb
    db_type = site_config.get('db_type', 'mariadb')
    is_database_restored = False

    if db_type == 'mariadb':
        restore_mariadb(
            config=config,
            site_config=site_config,
            database_file=database_file)
        is_database_restored = True
    elif db_type == 'postgres':
        restore_postgres(
            config=config,
            site_config=site_config,
            database_file=database_file)
        is_database_restored = True

    if is_database_restored:
        # Set encryption_key if it exists in backup site_config.json
        set_key_in_site_config('encryption_key', site, site_config_path)


def set_key_in_site_config(key, site, site_config_path):
    site_config = get_site_config_from_path(site_config_path)
    value = site_config.get(key)
    if value:
        print('Set {key} in site config for site: {site}'.format(key=key, site=site))
        update_site_config(key, value,
                            site_config_path=os.path.join(os.getcwd(), site, "site_config.json"))


def get_site_config_from_path(site_config_path):
    site_config = dict()
    if os.path.exists(site_config_path):
        with open(site_config_path, 'r') as sc:
            site_config = json.load(sc)
    return site_config


def restore_files(files_base):
    public_files = files_base + '-files.tar'
    # extract tar
    public_tar = tarfile.open(public_files)
    print('Extracting {}'.format(public_files))
    public_tar.extractall()


def restore_private_files(files_base):
    private_files = files_base + '-private-files.tar'
    private_tar = tarfile.open(private_files)
    print('Extracting {}'.format(private_files))
    private_tar.extractall()


def pull_backup_from_s3():
    check_environment_variables()

    # https://stackoverflow.com/a/54672690
    s3 = boto3.resource(
        's3',
        region_name=os.environ.get('REGION'),
        aws_access_key_id=os.environ.get('ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('SECRET_ACCESS_KEY'),
        endpoint_url=os.environ.get('ENDPOINT_URL')
    )

    bucket_dir = os.environ.get('BUCKET_DIR')
    bucket_name = os.environ.get('BUCKET_NAME')
    bucket = s3.Bucket(bucket_name)

    # Change directory to /home/frappe/backups
    os.chdir(get_backup_dir())

    backup_files = []
    sites = set()
    site_timestamps = set()
    download_backups = []

    for obj in bucket.objects.filter(Prefix=bucket_dir):
        backup_file = obj.key.replace(os.path.join(bucket_dir, ''), '')
        backup_files.append(backup_file)
        site_name, timestamp, backup_type = backup_file.split('/')
        site_timestamp = site_name + '/' + timestamp
        sites.add(site_name)
        site_timestamps.add(site_timestamp)

    # sort sites for latest backups
    for site in sites:
        backup_timestamps = []
        for site_timestamp in site_timestamps:
            site_name, timestamp = site_timestamp.split('/')
            if site == site_name:
                timestamp_datetime = datetime.datetime.strptime(
                    timestamp, DATE_FORMAT
                )
                backup_timestamps.append(timestamp)
        download_backups.append(site + '/' + max(backup_timestamps))

    # Only download latest backups
    for backup_file in backup_files:
        for backup in download_backups:
            if backup in backup_file:
                if not os.path.exists(os.path.dirname(backup_file)):
                    os.makedirs(os.path.dirname(backup_file))
                print('Downloading {}'.format(backup_file))
                bucket.download_file(bucket_dir + '/' + backup_file, backup_file)

    os.chdir(os.path.join(os.path.expanduser('~'), 'frappe-bench', 'sites'))


def restore_postgres(config, site_config, database_file):
    # common config
    common_site_config_path = os.path.join(os.getcwd(), COMMON_SITE_CONFIG_FILE)

    db_root_user = config.get('root_login')
    if not db_root_user:
        postgres_user = os.environ.get('DB_ROOT_USER')
        if not postgres_user:
            print('Variable DB_ROOT_USER not set')
            exit(1)

        db_root_user = postgres_user
        update_site_config(
            "root_login",
            db_root_user,
            validate=False,
            site_config_path=common_site_config_path)

    db_root_password = config.get('root_password')
    if not db_root_password:
        root_password = get_password('POSTGRES_PASSWORD')
        if not root_password:
            print('Variable POSTGRES_PASSWORD not set')
            exit(1)

        db_root_password = root_password
        update_site_config(
            "root_password",
            db_root_password,
            validate=False,
            site_config_path=common_site_config_path)

    # site config
    db_host = site_config.get('db_host')
    db_port = site_config.get('db_port', 5432)
    db_name = site_config.get('db_name')
    db_password = site_config.get('db_password')

    psql_command = "psql postgres://{root_login}:{root_password}@{db_host}:{db_port}".format(
        root_login=db_root_user,
        root_password=db_root_password,
        db_host=db_host,
        db_port=db_port
    )

    print('Restoring PostgreSQL')
    os.system(psql_command + ' -c "DROP DATABASE IF EXISTS \"{db_name}\""'.format(db_name=db_name))
    os.system(psql_command + ' -c "DROP USER IF EXISTS {db_name}"'.format(db_name=db_name))
    os.system(psql_command + ' -c "CREATE DATABASE \"{db_name}\""'.format(db_name=db_name))
    os.system(psql_command + ' -c "CREATE user {db_name} password \'{db_password}\'"'.format(
        db_name=db_name,
        db_password=db_password))
    os.system(psql_command + ' -c "GRANT ALL PRIVILEGES ON DATABASE \"{db_name}\" TO {db_name}"'.format(
        db_name=db_name))

    os.system("{psql_command}/{db_name} < {database_file}".format(
        psql_command=psql_command,
        database_file=database_file.replace('.gz', ''),
        db_name=db_name,
    ))


def restore_mariadb(config, site_config, database_file):
    db_root_password = get_password('MYSQL_ROOT_PASSWORD')
    if not db_root_password:
        print('Variable MYSQL_ROOT_PASSWORD not set')
        exit(1)

    db_root_user = os.environ.get("DB_ROOT_USER", 'root')

    db_host = site_config.get('db_host', config.get('db_host'))
    db_port = site_config.get('db_port', config.get('db_port'))

    # mysql command prefix
    mysql_command = 'mysql -u{db_root_user} -h{db_host} -p{db_password}'.format(
        db_root_user=db_root_user,
        db_host=db_host,
        db_port=db_port,
        db_password=db_root_password
    )

    # drop db if exists for clean restore
    drop_database = "{mysql_command} -e \"DROP DATABASE IF EXISTS \`{db_name}\`;\"".format(
        mysql_command=mysql_command,
        db_name=site_config.get('db_name'),
    )
    os.system(drop_database)

    # create db
    create_database = "{mysql_command} -e \"CREATE DATABASE IF NOT EXISTS \`{db_name}\`;\"".format(
        mysql_command=mysql_command,
        db_name=site_config.get('db_name'),
    )
    os.system(create_database)

    # create user
    create_user = "{mysql_command} -e \"CREATE USER IF NOT EXISTS \'{db_name}\'@\'%\' IDENTIFIED BY \'{db_password}\'; FLUSH PRIVILEGES;\"".format(
        mysql_command=mysql_command,
        db_name=site_config.get('db_name'),
        db_password=site_config.get('db_password'),
    )
    os.system(create_user)

    # grant db privileges to user
    grant_privileges = "{mysql_command} -e \"GRANT ALL PRIVILEGES ON \`{db_name}\`.* TO '{db_name}'@'%' IDENTIFIED BY '{db_password}'; FLUSH PRIVILEGES;\"".format(
        mysql_command=mysql_command,
        db_name=site_config.get('db_name'),
        db_password=site_config.get('db_password'),
    )
    os.system(grant_privileges)

    command = "{mysql_command} '{db_name}' < {database_file}".format(
        mysql_command=mysql_command,
        db_name=site_config.get('db_name'),
        database_file=database_file.replace('.gz', ''),
    )

    print('Restoring MariaDB')
    os.system(command)


def main():
    backup_dir = get_backup_dir()

    if len(list_directories(backup_dir)) == 0:
        pull_backup_from_s3()

    for site in list_directories(backup_dir):
        site_slug = site.replace('.', '_')
        backups = [datetime.datetime.strptime(backup, DATE_FORMAT) for backup in list_directories(os.path.join(backup_dir, site))]
        latest_backup = max(backups).strftime(DATE_FORMAT)
        files_base = os.path.join(backup_dir, site, latest_backup, '')
        files_base += latest_backup + '-' + site_slug
        site_config_path = files_base + '-site_config_backup.json'
        if not os.path.exists(site_config_path):
            site_config_path = os.path.join(backup_dir, site, 'site_config.json')
        if site in get_sites():
            print('Overwrite site {}'.format(site))
            restore_database(files_base, site_config_path, site)
            restore_private_files(files_base)
            restore_files(files_base)
        else:
            site_config = get_conf_params(
                db_name='_' + hashlib.sha1(site.encode()).hexdigest()[:16],
                db_password=random_string(16)
            )

            frappe.local.site = site
            frappe.local.sites_path = os.getcwd()
            frappe.local.site_path = os.getcwd() + '/' + site
            make_conf(
                db_name=site_config.get('db_name'),
                db_password=site_config.get('db_password'),
            )
            make_site_dirs()

            print('Create site {}'.format(site))
            restore_database(files_base, site_config_path, site)
            restore_private_files(files_base)
            restore_files(files_base)

    if frappe.redis_server:
        frappe.redis_server.connection_pool.disconnect()

    exit(0)


if __name__ == "__main__":
    main()
