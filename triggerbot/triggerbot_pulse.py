# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import re
import sys

from mozillapulse import consumers
from mozlog.structured import commandline

from .tree_watcher import TreeWatcher

logger = None
CONF_PATH = '../scratch/conf.json'
triggerbot_users = []
def trigger_bot_user(m):
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

    status = build_data['results']
    files = []
    comments = None
    user = None

    if 'sourceStamp' in build_data and len(build_data['sourceStamp'].get('changes')):
        change = build_data['sourceStamp']['changes'][-1]
        if 'files' in change:
            for f in change['files']:
                if 'name' in f and f['name']:
                    files.append(f['name'])
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

    return (branch, rev, builder, status,
            match is not None, files, comments, user)


def handle_message(data, message):

    message.ack()
    key = data['_meta']['routing_key']
    (branch, rev, builder, status,
     is_test, files, comments, user) = extract_payload(data['payload'], key)


    logger.info('%s %s' % (user, key))

    if not all([branch == 'try',
                is_test,
                trigger_bot_user(user)]):
        return

    logger.info('%s is a trigger bot user' % user)
    logger.info('Saw %s at %s with "%s"' % (user, rev, comments))
    logger.info('Files: %s' % files)

    tw.handle_message(key, branch, rev, builder, status, comments,
                      files)


def read_pulse_auth():
    with open(CONF_PATH) as f:
        conf = json.load(f)
        return conf['pulse_user'], conf['pulse_pw']

def read_ldap_auth():
    with open(CONF_PATH) as f:
        conf = json.load(f)
        return conf['ldap_user'], conf['ldap_pw']

def get_users():
    global triggerbot_users
    with open(CONF_PATH) as f:
        conf = json.load(f)
        triggerbot_users = conf['triggerbot_users']


def run():

    global logger
    global tw

    parser = argparse.ArgumentParser()
    commandline.add_logging_group(parser)
    args = parser.parse_args(sys.argv[1:])
    service_name = 'trigger-bot'
    logger = commandline.setup_logging(service_name,
                                       args,
                                       {
                                           'mach': sys.stdout,
                                       })
    logger.info('starting listener')
    ldap_auth = read_ldap_auth()
    tw = TreeWatcher(ldap_auth)
    user, pw = read_pulse_auth()
    get_users()
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
            logger.error("Received an unexpected exception", exc_info=True)
