# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import re
import sys
import time
import threading
import requests

from collections import defaultdict


from mozillapulse import consumers
from mozlog.structured import commandline, get_default_logger


logger = get_default_logger()
conf_path = '../scratch/conf.json'
triggerbot_users = []
def trigger_bot_user(m):
    return m in triggerbot_users
tw = None


class TryWatcher(object):
    """Class to keep track of the triggers we've already done and
    those yet to do for a particular rev/tree.
    """
    # Don't trigger more than this many jobs for a rev.
    # Arbitrary limit: if orange factor is around 6, and we re-trigger
    # for each orange, we shouldn't need to trigger much more than that for
    # any push that would be suitable to land.
    default_retry = 1
    per_push_failures = 6
    # This is... also quite arbitrary. See the comment below about pruning
    # old revisions.
    revmap_threshold = 2000
    requested_limit = 20

    def __init__(self, ldap_auth):
        self.revmap = defaultdict(dict)
        self.revmap_threshold = TryWatcher.revmap_threshold
        self.auth = ldap_auth
        self.trigger_limit = TryWatcher.default_retry * TryWatcher.per_push_failures

    def known_rev(self, branch, rev):
        return rev in self.revmap

    def _prune_revmap(self):
        # After a certain point we'll need to prune our revmap so it doesn't grow
        # infinitely.
        # We only need to keep an entry around from when we last see it
        # as an incoming revision and the next time it's finished and potentially
        # failed, but it could be pending for a while so we don't know how long that
        # will be.
        target_count = int(TryWatcher.revmap_threshold * 2/3)
        prune_count = len(self.revmap.keys()) - target_count
        logger.info('Pruning %d entries from the revmap' % prune_count)

        # Could/should use an LRU cache here, but assuming any job will go
        # from pending to complete in 24 hrs and we have up to 528 pushes a
        # day (like we had last April fool's day), that's still just 528
        # entries to sort.
        for rev, data in sorted(self.revmap.items(), key=lambda (k, v): v['time_seen']):
            if not prune_count:
                logger.info('Finished pruning, oldest rev is now: %s' %
                            rev)
                return

            del self.revmap[rev]
            prune_count -= 1


    def failure_trigger(self, branch, rev, builder):
        logger.info('Found a failure for %s and may retrigger' % rev)

        if rev in self.revmap:

            if 'fail_retrigger' not in self.revmap[rev]:
                return

            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                logger.info('We\'ve already triggered "%s" at %s and don\'t'
                            ' need to do it again' % (builder, rev))
                return

            seen_builders.add(builder)
            count = self.revmap[rev]['fail_retrigger']
            seen = self.revmap[rev]['rev_trigger_count']

            if seen >= self.trigger_limit:
                logger.info('Would have triggered "%s" at %s but there are already '
                            'too many failures.' % (builder, rev))
                return

            self.revmap[rev]['rev_trigger_count'] += count
            self.trigger_n_times(branch, rev, builder, count)
            logger.warning('Triggering %d of "%s" at %s' % (count, builder, rev))
            logger.warning('Already triggered %d for %s' % (seen, rev))


    def requested_trigger(self, branch, rev, builder):
        if rev in self.revmap and 'requested_trigger' in self.revmap[rev]:

            logger.info('Found a request to trigger %s and may retrigger' % rev)
            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                logger.info('We already triggered "%s" at %s don\'t need'
                            ' to do it again' % (builder, rev))
                return

            seen_builders.add(builder)
            count = self.revmap[rev]['requested_trigger']
            logger.info('Triggering %d requested jobs for "%s" at %s' %
                        (count, builder, rev))
            self.trigger_n_times(branch, rev, builder, count)


    def add_rev(self, branch, rev, comments, files):

        req_count = self.trigger_count_from_msg(comments)

        if req_count:
            logger.info('Added %d triggers for %s' % (req_count, rev))
            self.revmap[rev]['requested_trigger'] = req_count
        else:
            logger.info('Adding default failure retries for %s' % rev)
            self.revmap[rev]['fail_retrigger'] = TryWatcher.default_retry

        self.revmap[rev]['rev_trigger_count'] = 0
        self.revmap[rev]['comments'] = comments
        self.revmap[rev]['files'] = files
        # When we need to purge old revisions, we need to purge the
        # oldest first.
        self.revmap[rev]['time_seen'] = time.time()

        # Prevent an infinite retrigger loop - if we take a trigger action,
        # ensure we only take it once for a builder on a particular revision.
        self.revmap[rev]['seen_builders'] = set()

        if len(self.revmap.keys()) > self.revmap_threshold:
            self._prune_revmap()


    def trigger_count_from_msg(self, msg):

        try_message = None
        all_try_args = None

        for line in msg.splitlines():
            if 'try: ' in line:
                # Allow spaces inside of [filter expressions]
                try_message = line.strip().split('try: ', 1)
                all_try_args = re.findall(r'(?:\[.*?\]|\S)+', try_message[1])
                break

        if not try_message:
            return 0

        parser = argparse.ArgumentParser()
        parser.add_argument('--rebuild', type=int, default=0)
        (args, _) = parser.parse_known_args(all_try_args)

        limit = TryWatcher.requested_limit
        return args.rebuild if args.rebuild < limit else limit


    def handle_message(self, key, branch, rev, builder, status,
                       comments, files):
        if not self.known_rev(branch, rev) and comments:
            # First time we've seen this revision? Add it to known
            # revs and mark required triggers,
            self.add_rev(branch, rev, comments, files)

        if key.endswith('started'):
            # If the job is starting and a user requested unconditional
            # retriggers, process them right away.
            self.requested_trigger(branch, rev, builder)

        if status in (1, 2):
            # A failing job is a candidate to retrigger.
            self.failure_trigger(branch, rev, builder)


    def trigger_n_times(self, branch, rev, builder, count):

        if not re.match("[a-z0-9]{12,40}", rev):
            logger.error("%s doesn't look like a valid revision, can't trigger it")
            return

        root_url = 'https://secure.pub.build.mozilla.org/buildapi/self-serve'
        tmpl = '%s/%s/builders/%s/%s'

        trigger_url = tmpl % (root_url, branch, builder, rev)
        logger.info('Triggering url: %s' % trigger_url)

        payload = {
            'branch': branch,
            'revision': rev,
            # Why do we need to double quote these fields?
            'files': json.dumps(self.revmap[rev]['files']),
            'properties': json.dumps({
                'try_syntax': self.revmap[rev]['comments'],
            }),
        }
        logger.debug('Triggering payload:\n\t%s' % payload)

        for i in range(count):
            req = requests.post(
                trigger_url,
                headers={'Accept': 'application/json'},
                data=payload,
                auth=self.auth
            )
            logger.info('Requested job, return: %s' % req.status_code)

        import pdb; pdb.set_trace()


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

    with open(conf_path) as f:
        conf = json.load(f)
        return conf['pulse_user'], conf['pulse_pw']


def read_ldap_auth():

    with open(conf_path) as f:
        conf = json.load(f)
        return conf['ldap_user'], conf['ldap_pw']

def get_users():
    global triggerbot_users
    with open(conf_path) as f:
        conf = json.load(f)
        triggerbot_users = conf['triggerbot_users']


def run():

    global logger
    global tw

    parser = argparse.ArgumentParser()
    commandline.add_logging_group(parser)
    args = parser.parse_args(sys.argv[1:])
    service_name = 'mozci-trigger-bot'
    logger = commandline.setup_logging(service_name,
                                       args,
                                       {
                                           'mach': sys.stdout,
                                       })
    logger.info('starting listener')
    ldap_auth = read_ldap_auth()
    tw = TryWatcher(ldap_auth)
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
