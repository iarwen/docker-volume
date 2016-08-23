#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import boto3
import datetime
import glob
import json
import logging
import os
import re
import shutil
import signal
import SimpleHTTPServer
import SocketServer
import subprocess
import sys
import tarfile
import urlparse
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def should_exclude(filename, exclude_list):
    for exclude in exclude_list:
        if re.search(exclude, filename):
            return True
    return False


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', default=8000, type=int)
    parser.add_argument('--no-restore', action='store_true')
    parser.add_argument('--no-backup', action='store_true')
    return parser.parse_args()


class Volume(object):
    def __init__(self, config_path):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_path = config_path
        signal.signal(signal.SIGINT, self.signal)
        signal.signal(signal.SIGTERM, self.signal)

    def read_config(self, config_path):
        parts = urlparse.urlparse(config_path)
        if parts.scheme in ('http', 'https', 'ftp', 'file'):
            import urllib2
            response = urllib2.urlopen(config_path)
            config_content = response.read()
        elif parts.scheme == 's3':
            client = boto3.client('s3')
            response = client.get_object(Bucket=parts.netloc,
                                         Key=parts.path[1:])
            config_content = response['Body'].read()
        else:
            raise RuntimeError("Not supported scheme: {0}".format(config_path))
        config = json.loads(config_content)
        if not 'tmp' in config:
            config['tmp'] = '/tmp'
        return config

    def backup(self):
        config = self.read_config(self.config_path)
        suffix = datetime.datetime.now().strftime("-%Y%m%d-%H%M%S") + '.tar.gz'
        for backup in config['backups']:
            if 'path' not in backup:
                continue
            path = backup['path']
            exclude_list = backup.get('exclude', [])
            dest = backup['dest']
            parts = urlparse.urlparse(dest)
            if parts.scheme not in ('s3', 'file'):
                raise RuntimeError("Not supported scheme: {0}".format(dest))
            backup_file = parts.path + suffix
            self.logger.info("Start backup: %s", path)
            tar_file = os.path.join(config['tmp'],
                                    os.path.basename(backup_file))
            tar = tarfile.open(tar_file, 'w:gz')
            try:
                for root, dirs, files in os.walk(path):
                    for f in files + dirs:
                        if root == path:
                            arcname = f
                        else:
                            arcname = os.path.join(root[len(path)+1:], f)
                        if not should_exclude(arcname,
                                              exclude_list):
                            try:
                                tar.add(os.path.join(root, f),
                                        arcname=arcname,
                                        recursive=False)
                            except IOError:
                                pass
                tar.close()
                if parts.scheme == 's3':
                    s3_params = backup.get('s3', {})
                    client = boto3.client('s3')
                    self.logger.info("Uploading %s to %s/%s",
                                     tar_file, parts.netloc, backup_file[1:])
                    client.upload_file(tar_file, parts.netloc, backup_file[1:],
                                       ExtraArgs=s3_params)
                elif parts.scheme == 'file':
                    dest_path = os.path.join(parts.netloc, backup_file)
                    self.logger.info("Copying %s to %s",
                                     tar_file, dest_path)
                    try:
                        dirname = os.path.dirname(dest_path)
                        if not os.path.exists(dirname):
                            os.makedirs(dirname)
                        shutil.copyfile(tar_file, dest_path)
                    except IOError as err:
                        self.logger.error("Failed to copy: {0}".format(str(err)))
            finally:
                if os.path.exists(tar_file):
                    os.remove(tar_file)
            self.logger.info("Done backup: %s", path)

    def restore(self):
        config = self.read_config(self.config_path)
        for backup in config['backups']:
            if 'path' not in backup:
                continue
            path = backup['path']
            self.logger.info("Restoring to {0}".format(path))
            if not os.path.exists(path):
                os.makedirs(path)
            if 'chmod' in backup:
                self.logger.info("chmod {0}".format(backup['chmod']))
                subprocess.call(['chmod', backup['chmod'], path])
            if 'chown' in backup:
                self.logger.info("chown {0}".format(backup['chown']))
                subprocess.call(['chown', backup['chown'], path])
            dest = backup['dest']
            parts = urlparse.urlparse(dest)
            tar_file = None
            try:
                if parts.scheme == 's3':
                    client = boto3.client('s3')
                    objects = client.list_objects(Bucket=parts.netloc,
                                                  Prefix=parts.path[1:])
                    if 'Contents' in objects:
                        keys = sorted([c['Key'] for c in objects['Contents']])
                        if keys:
                            key = keys[-1]
                            tar_file = os.path.join(config['tmp'],
                                                    os.path.basename(key))
                            client.download_file(parts.netloc, key, tar_file)
                elif parts.scheme == 'file':
                    src_file = os.path.join(parts.netloc, parts.path)
                    files = sorted(glob.glob(src_file + '*'))
                    if files:
                        filename = files[-1]
                        try:
                            tar_file = os.path.join(config['tmp'], os.path.basename(filename))
                            shutil.copyfile(filename, tar_file)
                        except IOError as err:
                            self.logger.error("Failed to copy: {0}".format(str(err)))
                else:
                    raise RuntimeError("Not supported scheme: {0}".
                                       format(dest))
                if tar_file is not None:
                    self.logger.info("Restoring from {0}".format(tar_file))
                    tar = tarfile.open(tar_file, 'r:gz')
                    tar.extractall(backup['path'])
                    tar.close()
            finally:
                if tar_file is not None:
                    if os.path.exists(tar_file):
                        os.remove(tar_file)

    def signal(self, sig, stack):
        self.logger.info("Recieved signal: %d", sig)
        raise SystemExit('Exiting')


class ServerHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.wfile.write("GET\n")
        self.send_response(200)

    def do_POST(self):
        self.log_message('POST recieved')
        try:
            self.server.volume.backup()
            self.wfile.write("BACKUP DONE\n")
            self.send_response(200)
        except Exception as err:
            import traceback
            self.wfile.write(traceback.format_exc())
            self.send_response(500)
            raise


class Server(SocketServer.TCPServer):
    allow_reuse_address = True

args = get_args()
volume = Volume(args.config)
if not args.no_restore:
    volume.restore()

Handler = ServerHandler
httpd = Server(("", args.port), Handler)
httpd.volume = volume

logger.info("Server started port:%d", args.port)
try:
    httpd.serve_forever()
finally:
    if not args.no_backup:
        volume.backup()
    logger.info("Finished")
