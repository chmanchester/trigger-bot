# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import logging
import os
import re
import sys

from mozillapulse import consumers

from .tree_watcher import TreeWatcher

logger = None
CONF_PATH = '../scratch/conf.json'
triggerbot_users = []
def is_triggerbot_user(m):
    return m in triggerbot_users
tw = None


def extract_payload(payload, key):

    branch = None
    rev = None
    builder = None
    build_data = payload['build']

    for prop in build_data['properties']:
        if prop[0] == 'revision':
            rev = prop[1]
        if prop[0] == 'buildername':
            builder = prop[1]
        if prop[0] == 'branch':
            branch = prop[1]
        if prop[0] == 'platform':
            platform = prop[1]

    if rev and len(rev) > 12:
        rev = rev[:12]

    status = build_data['results']
    comments = None
    user = None

    if 'sourceStamp' in build_data and len(build_data['sourceStamp'].get('changes')):
        change = build_data['sourceStamp']['changes'][-1]
        if 'comments' in change and 'try:' in change['comments']:
            comments = change['comments']
        if 'who' in change:
            user = change['who']

    # See if this is a unit test (borrowed from the pulsetranslator).
    # Pretty terrible, but test start is necessary (and ignored by
    # the normalized build exchange).
    unittest_re = re.compile(r'build\.((%s)[-|_](.*?)(-debug|-o-debug|-pgo|_pgo|_test)?[-|_](test|unittest|pgo)-(.*?))\.(\d+)\.(started|finished)' %
                             branch)
    match = unittest_re.match(key)

    # Bug 1208104: Force triggering off for Windows testers until
    # we have control of the backlog.
    if platform in ('win64', 'win32'):
        match = None

    return branch, rev, builder, status, match is not None, comments, user


def handle_message(data, message):

    message.ack()
    key = data['_meta']['routing_key']
    (branch, rev, builder, status,
     is_test, comments, user) = extract_payload(data['payload'], key)

    if not all([branch == 'try',
                is_test]):
        return

    tw.handle_message(key, branch, rev, builder, status, comments, user)


def read_pulse_auth():
    if os.environ.get('TB_PULSE_USERNAME') and os.environ.get('TB_PULSE_PW'):
        return os.environ['TB_PULSE_USERNAME'], os.environ['TB_PULSE_PW']
    with open(CONF_PATH) as f:
        conf = json.load(f)
        return conf['pulse_user'], conf['pulse_pw']

def read_ldap_auth():
    if os.environ.get('TB_LDAP_USERNAME') and os.environ.get('TB_LDAP_PW'):
        return os.environ['TB_LDAP_USERNAME'], os.environ['TB_LDAP_PW']
    with open(CONF_PATH) as f:
        conf = json.load(f)
        return conf['ldap_user'], conf['ldap_pw']

def get_users():
    global triggerbot_users
    if os.environ.get('TB_USERS'):
        triggerbot_users = os.environ['TB_USERS'].split()
        return
    with open(CONF_PATH) as f:
        conf = json.load(f)
        triggerbot_users = conf['triggerbot_users']

def setup_logging(name, log_dir, log_stderr):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(levelname)s: %(message)s")

    if log_dir:
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)

        filename = os.path.join(log_dir, name)

        handler = logging.handlers.RotatingFileHandler(
            filename, mode='a+', maxBytes=1000000, backupCount=3)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if log_stderr:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger

def run():

    global logger
    global tw
    global is_triggerbot_user

    parser = argparse.ArgumentParser()
    parser.add_argument('--log-dir')
    parser.add_argument('--no-log-stderr', dest='log_stderr',
                        action='store_false', default=True)
    args = parser.parse_args(sys.argv[1:])
    service_name = 'trigger-bot'
    logger = setup_logging(service_name, args.log_dir, args.log_stderr)
    logger.info('starting listener')

    ldap_auth = read_ldap_auth()

    user, pw = read_pulse_auth()
    get_users()

    tw = TreeWatcher(ldap_auth)

    consumer = consumers.BuildConsumer(applabel=service_name,
                                       user=user,
                                       password=pw)
    consumer.configure(topic=['build.#.started', 'build.#.finished'],
                       callback=handle_message)

    while True:
        try:
            consumer.listen()
        except KeyboardInterrupt:
            raise
        except IOError:
            pass
        except:
            logger.exception("Received an unexpected exception")
