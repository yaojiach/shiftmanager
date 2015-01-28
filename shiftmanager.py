#!/usr/bin/env python

import argparse

import oauth2client
from oauth2client import tools
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage

import httplib2
import apiclient.discovery
import apiclient.http
import apiclient.errors

import psycopg2
import gnupg

import string
import random
import os
from tempfile import NamedTemporaryFile
from subprocess import check_output

# OAuth 2.0 scope that will be authorized.
# Check https://developers.google.com/drive/scopes for all available scopes.
OAUTH2_SCOPE = 'https://www.googleapis.com/auth/drive'

# Location of the client secrets.
CLIENT_SECRETS = 'data/client_secrets.json'

# Local access token file
STORAGE_FILENAME = 'access_token.json'

# Metadata about the file.
MIMETYPE = 'text/plain'
TITLE = 'Redshift Creds - {email}'
DESCRIPTION = 'Instructions and creds for accessing the Redshift cluster'

# Redshift distribution styles
DISTSTYLES_BY_INDEX = {
    0 : 'EVEN',
    1 : 'KEY',
    8 : 'ALL',
}

# Database hosts
DB_HOSTS = {
    'prod': 'prod-data-pipeline.cuxrn97vbxid.us-east-1.redshift.amazonaws.com',
    'dev':  'dev-data-pipeline.cuxrn97vbxid.us-east-1.redshift.amazonaws.com',
}

# Credentials text file template
CREDSFILE_TEMPLATE = """\
Redshift credentials for {gdrive_username}@simple.com

username:
{redshift_username}

password:
{password}

You have accounts both on the production cluster:
    prod-data-pipeline.cuxrn97vbxid.us-east-1.redshift.amazonaws.com
And the dev cluster:
   dev-data-pipeline.cuxrn97vbxid.us-east-1.redshift.amazonaws.com

The port for both clusters is 5439.

For accessing Redshift through command-line tools, you may want to set
the following environment variables:

PGUSER={redshift_username}
PGPASSWORD={password}
PGHOST=prod-data-pipeline.cuxrn97vbxid.us-east-1.redshift.amazonaws.com
PGPORT=5439
"""

SERVICE_S3_UPLOAD_REQUEST_TEMPLATE = """\
We'd like to get some PGPASSWORD files into S3 for {service_name}:

echo "{devpass}" > PGPASSWORD
aws s3 cp PGPASSWORD s3://com-simple-dev/credentials/{service_name}/service-keys/PGPASSWORD

echo "{prodpass}" > PGPASSWORD
aws s3 cp PGPASSWORD s3://com-simple-prod/credentials/{service_name}/service-keys/PGPASSWORD
"""  # noqa

S3_UPLOADER_FINGERPRINTS = [
    "E034918ABA561DD06CAF46D8C4721EA079FF66A9",  # security@simple
    "4D51E7FEA4886C3A6BAA3A4C09A69F598386962C",  # moyer
    "C066921AF167318A937C104DBD221018E2BD32EF",  # mehlert
    "BF7F08BCCB9A9427094E32876DBA10920728A5D5",  # steven
]


class TableDefinitionStatement(object):
    """
    Container for pulling a table definition from Redshift and modifying it.
    """
    def __init__(self, conn, tablename, distkey=None, sortkey=None, diststyle=None, owner=None):
        """
        Pulls information from Redshift using the `pg_dump` executable
        and queries to system tables (via *conn*).
        """
        self.tablename = tablename

        output = check_output(['pg_dump', '--schema-only', '--table', tablename, 'analytics'])
        lines = output.split('\n')
        self.sets = [line for line in lines
                     if line.startswith('SET ')]
        self.grants = [line for line in lines
                       if line.startswith('REVOKE ')
                       or line.startswith('GRANT ')]
        self.alters = [line for line in lines
                       if line.startswith('ALTER TABLE ')]
        if owner:
            self.alters.append('ALTER TABLE {0} OWNER to {1};'.format(tablename, owner))
            self.grants.append('GRANT ALL ON TABLE {0} TO {1};'.format(tablename, owner))
        create_start_index = [i for i, line in enumerate(lines)
                              if line.startswith('CREATE TABLE ')][0]
        create_end_index = [i for i, line in enumerate(lines)
                            if i > create_start_index
                            and line.startswith(');')][0]
        self.create_lines = lines[create_start_index : create_end_index]

        cur = conn.cursor()
        query_template = """
        SELECT \"column\" from pg_table_def
        WHERE tablename = '{0}'
        AND {1} = {2}
        """
        cur.execute(query_template.format(tablename, 'distkey', "'t'"))
        result = cur.fetchall()
        self.distkey = distkey or result and result[0][0] or None
        cur.execute(query_template.format(tablename, 'sortkey', "1"))
        result = cur.fetchall()
        self.sortkey = sortkey or result and result[0][0] or None

        query_template = """
        SELECT reldiststyle FROM pg_class
        WHERE relname = '{0}'
        """
        cur.execute(query_template.format(tablename))
        self.diststyle = diststyle or DISTSTYLES_BY_INDEX[cur.fetchone()[0]]
        if distkey and not diststyle:
            self.diststyle = 'KEY'
        if self.diststyle.upper() in ['ALL', 'EVEN']:
            self.distkey = None

    def definition(self):
        """
        Returns the full SQL code to recreate the table.
        """
        names = {
            'tablename' : self.tablename,
            'srcname' : 'tempsource_' + self.tablename,
            'tgtname' : 'temptarget_' + self.tablename,
        }

        create_lines = self.create_lines[:]
        create_lines[0] = create_lines[0].replace(self.tablename, '{tgtname}')
        create_lines.append(") DISTSTYLE {0}".format(self.diststyle))
        if self.distkey:
            create_lines.append("  DISTKEY({0})".format(self.distkey))
        if self.sortkey:
            create_lines.append("  SORTKEY({0})".format(self.sortkey))
        create_lines[-1] += ';\n'

        all_lines = (
            create_lines +
            ["ALTER TABLE {tablename} RENAME TO {srcname};"] +
            ["INSERT INTO {tgtname} (SELECT * FROM {srcname});"] +
            ["ALTER TABLE {tgtname} RENAME TO {tablename};"] +
            ["DROP TABLE {srcname};\n"] +
            self.alters +
            [''] +
            self.grants)
        for i, line in enumerate(all_lines):
            all_lines[i] = '  ' + line
        all_lines.insert(0, 'BEGIN TRANSACTION;\n')
        all_lines.append('\nEND TRANSACTION;')

        return '\n'.join(all_lines).format(**names)



def connect_from_env():
    """
    Return a psycopg2 connection based on environment variables.
    """
    return psycopg2.connect(
        host = os.environ['PGHOST'],
        database = os.environ.get('PGDATABASE', 'analytics'),
        port = int(os.environ.get('PGPORT', 5439)),
    )

def _get_host(host):
    if host in DB_HOSTS:
        return DB_HOSTS[host]
    return host


def random_password(length=64):
    """
    Return a strong and valid password for Redshift.

    Constraints:
     - 8 to 64 characters in length.
     - Must contain at least one uppercase letter, one lowercase letter,
       and one number.
     - Can use any printable ASCII characters (ASCII code 33 to 126)
       except ' (single quote), \" (double quote), \\, /, @, or space.
     - See http://docs.aws.amazon.com/redshift/latest/dg/r_CREATE_USER.html

    """
    rand = random.SystemRandom()
    invalid_chars = r'''\/'"@ '''
    valid_chars_set = set(
        string.digits +
        string.letters +
        string.punctuation
    ) - set(invalid_chars)
    valid_chars = list(valid_chars_set)
    chars = [rand.choice(string.ascii_uppercase),
             rand.choice(string.ascii_lowercase),
             rand.choice(string.digits)]
    chars += [rand.choice(valid_chars) for x in xrange(length - 3)]
    rand.shuffle(chars)
    return ''.join(chars)


def create_user(host, username, password):
    """
    Create a new user account.
    """

    host = _get_host(host)

    conn = psycopg2.connect(
        host=host,
        database='analytics',
        port=5439,
    )

    cur = conn.cursor()

    cur.execute("""
    CREATE USER {0}
    PASSWORD '{1}'
    IN GROUP analyticsusers;
    """.format(username, password))

    conn.commit()


def set_password(host, username, password):
    """
    Set a user's password.
    """

    host = _get_host(host)

    conn = psycopg2.connect(
        host=host,
        database='analytics',
        port=5439,
    )

    cur = conn.cursor()

    cur.execute("""
    ALTER USER {0}
    PASSWORD '{1}';
    """.format(username, password))

    conn.commit()


def post_user_creds_to_gdrive(gdrive_username, redshift_username, password):
    """
    Uploads a text file to Google Drive, shared with *gdrive_username*.

    The text is taken from a template, with usernames and passwords inserted
    as appropriate.
    """

    # In case the user included '@simple.com' in the username
    gdrive_username = gdrive_username.split('@')[0]

    email = '{0}@simple.com'.format(gdrive_username)

    flow = flow_from_clientsecrets(
        CLIENT_SECRETS,
        scope=OAUTH2_SCOPE,
        redirect_uri=oauth2client.client.OOB_CALLBACK_URN)

    storage = Storage(STORAGE_FILENAME)

    # Yeah, this isn't a command-line app, but this argparse flow was
    # the easiest method available.
    parser = argparse.ArgumentParser(parents=[tools.argparser])
    flags = parser.parse_args()

    credentials = storage.get()
    if credentials is None or credentials.invalid:
        credentials = tools.run_flow(flow, storage, flags)

    # Create an authorized Drive API client.
    http = httplib2.Http()
    credentials.authorize(http)
    drive_service = apiclient.discovery.build('drive', 'v2', http=http)

    # The body contains the metadata for the file.
    body = {
        'title': TITLE.format(**locals()),
        'description': DESCRIPTION.format(**locals()),
    }

    with NamedTemporaryFile() as gdoc:

        gdoc.write(CREDSFILE_TEMPLATE.format(**locals()))
        # Seek to the beginning so that there's something to read.
        gdoc.seek(0)

        # Insert a file. Files are comprised of contents and metadata.
        # MediaFileUpload abstracts uploading file contents from a
        # file on disk.
        media_body = apiclient.http.MediaFileUpload(
            gdoc.name,
            mimetype=MIMETYPE,
            resumable=True,
        )

        # Perform the request.
        new_file = drive_service.files().insert(
            body=body,
            media_body=media_body
        ).execute()

    # Add the target user as a reader.
    new_permission = {
        'value': email,
        'type': 'user',
        'role': 'reader',
    }
    drive_service.permissions().insert(
        fileId=new_file['id'],
        body=new_permission,
        emailMessage="",
    ).execute()

    print ("Successfully created creds for user '{0}' "
           "and sent a notification email.").format(redshift_username)


def encrypted_for_s3_uploaders(text):
    """
    Returns *text* encrypted with the public keys of folks known
    to have S3 upload privileges.
    """

    gpg = gnupg.GPG()

    keys = [key for key in gpg.list_keys()
            if key['fingerprint'] in S3_UPLOADER_FINGERPRINTS]

    print("Encypting for the following uids:")
    for key in keys:
        for uid in key['uids']:
            print("    " + uid)

    encrypted_data = gpg.encrypt(
        text,
        S3_UPLOADER_FINGERPRINTS,
        always_trust=True)

    return str(encrypted_data)


def text_for_service_cred_upload_request(service_name, devpass, prodpass):
    """
    Returns a string containing a request to security folks to upload
    passwords to appropriate locations in S3.
    """
    return SERVICE_S3_UPLOAD_REQUEST_TEMPLATE.format(**locals())
