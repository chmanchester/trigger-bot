# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import logging
import re
import time
import requests

from collections import defaultdict


class TreeWatcher(object):
    """Class to keep track of test jobs starting and finishing, known
    revisions and builders, and re-trigger jobs in either when a job
    fails or a when requested by a user.

    Redundant triggers are prevented by keeping track of each buildername,
    tree, revision we've already triggered. The invariant is that for
    any (buildername, tree, revision) combination, we will only issue triggers
    once. Old revisions are purged after a certain interval, so care must
    be taken that enough revisions are stored at a time to prevent issuing
    redundant triggers.
    """
    # Don't trigger more than this many jobs for a rev.
    # Arbitrary limit: if orange factor is around 5, and we re-trigger
    # for each orange, we shouldn't need to trigger much more than that for
    # any push that would be suitable to land.
    default_retry = 2
    per_push_failures = 5
    # This is... also quite arbitrary. See the comment below about pruning
    # old revisions.
    revmap_threshold = 2000
    # If someone asks for more than 20 rebuilds on a push, only give them 20.
    requested_limit = 20

    def __init__(self, ldap_auth, is_triggerbot_user=lambda _: True):
        self.revmap = defaultdict(dict)
        self.revmap_threshold = TreeWatcher.revmap_threshold
        self.auth = ldap_auth
        self.trigger_limit = TreeWatcher.default_retry * TreeWatcher.per_push_failures
        self.log = logging.getLogger('trigger-bot')
        self.is_triggerbot_user = is_triggerbot_user

    def _prune_revmap(self):
        # After a certain point we'll need to prune our revmap so it doesn't grow
        # infinitely.
        # We only need to keep an entry around from when we last see it
        # as an incoming revision and the next time it's finished and potentially
        # failed, but it could be pending for a while so we don't know how long that
        # will be.
        target_count = int(TreeWatcher.revmap_threshold * 2/3)
        prune_count = len(self.revmap.keys()) - target_count
        self.log.info('Pruning %d entries from the revmap' % prune_count)

        # Could/should use an LRU cache here, but assuming any job will go
        # from pending to complete in 24 hrs and we have up to 528 pushes a
        # day (like we had last April fool's day), that's still just 528
        # entries to sort.
        for rev, data in sorted(self.revmap.items(), key=lambda (k, v): v['time_seen']):
            if not prune_count:
                self.log.info('Finished pruning, oldest rev is now: %s' %
                            rev)
                return

            del self.revmap[rev]
            prune_count -= 1

    def known_rev(self, branch, rev):
        return rev in self.revmap

    def failure_trigger(self, branch, rev, builder):
        self.log.info('Found a failure for %s and may retrigger' % rev)

        if rev in self.revmap:

            if 'fail_retrigger' not in self.revmap[rev]:
                self.log.info('Found no request to retrigger %s on failure' %
                            rev)
                return

            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                self.log.info('We\'ve already triggered "%s" at %s and don\'t'
                            ' need to do it again' % (builder, rev))
                return

            seen_builders.add(builder)
            count = self.revmap[rev]['fail_retrigger']
            seen = self.revmap[rev]['rev_trigger_count']

            if seen >= self.trigger_limit:
                self.log.warning('Would have triggered "%s" at %s but there are already '
                               'too many failures.' % (builder, rev))
                return

            self.revmap[rev]['rev_trigger_count'] += count
            self.log.warning('Triggering %d of "%s" at %s' % (count, builder, rev))
            self.log.warning('Already triggered %d for %s' % (seen, rev))
            self.trigger_n_times(branch, rev, builder, count)


    def requested_trigger(self, branch, rev, builder):
        if rev in self.revmap and 'requested_trigger' in self.revmap[rev]:

            self.log.info('Found a request to trigger %s and may retrigger' % rev)
            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                self.log.info('We already triggered "%s" at %s don\'t need'
                            ' to do it again' % (builder, rev))
                return

            seen_builders.add(builder)
            count = self.revmap[rev]['requested_trigger']
            self.log.info('May trigger %d requested jobs for "%s" at %s' %
                        (count, builder, rev))
            self.trigger_n_times(branch, rev, builder, count)


    def add_rev(self, branch, rev, comments, files, user):

        req_count = self.trigger_count_from_msg(comments)

        if req_count:
            self.log.info('Added %d triggers for %s' % (req_count, rev))
            self.revmap[rev]['requested_trigger'] = req_count
        else:
            self.log.info('Adding default failure retries for %s' % rev)
            self.revmap[rev]['fail_retrigger'] = TreeWatcher.default_retry

        self.revmap[rev]['rev_trigger_count'] = 0
        self.revmap[rev]['comments'] = comments
        self.revmap[rev]['files'] = files

        # When we need to purge old revisions, we need to purge the
        # oldest first.
        self.revmap[rev]['time_seen'] = time.time()

        # Prevent an infinite retrigger loop - if we take a trigger action,
        # ensure we only take it once for a builder on a particular revision.
        self.revmap[rev]['seen_builders'] = set()

        # Filter triggering activity based on users.
        self.revmap[rev]['user'] = user

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

        limit = TreeWatcher.requested_limit
        return args.rebuild if args.rebuild < limit else limit


    def handle_message(self, key, branch, rev, builder, status,
                       comments, files, user):
        if not self.known_rev(branch, rev) and comments:
            # First time we've seen this revision? Add it to known
            # revs and mark required triggers,
            self.add_rev(branch, rev, comments, files, user)

        if key.endswith('started'):
            # If the job is starting and a user requested unconditional
            # retriggers, process them right away.
            self.requested_trigger(branch, rev, builder)

        if status in (1, 2):
            # A failing job is a candidate to retrigger.
            self.failure_trigger(branch, rev, builder)


    def trigger_n_times(self, branch, rev, builder, count):
        if not re.match("[a-z0-9]{12}", rev):
            self.log.error("%s doesn't look like a valid revision, can't trigger it")
            return

        if not self.is_triggerbot_user(self.revmap[rev]['user']):
            self.log.warning('Would have triggered "%s" at %s %d times.' %
                             (builder, rev, count))
            self.log.warning('But %s is not a triggerbot user.' % self.revmap[rev]['user'])
            return

        root_url = 'https://secure.pub.build.mozilla.org/buildapi/self-serve'
        tmpl = '%s/%s/builders/%s/%s'

        trigger_url = tmpl % (root_url, branch, builder, rev)
        self.log.info('Triggering url: %s' % trigger_url)

        payload = {
            'branch': branch,
            'revision': rev,
            'files': json.dumps(self.revmap[rev]['files']),
            'properties': json.dumps({
                'try_syntax': self.revmap[rev]['comments'],
            }),
        }
        self.log.debug('Triggering payload:\n\t%s' % payload)

        for i in range(count):
            req = requests.post(
                trigger_url,
                headers={'Accept': 'application/json'},
                data=payload,
                auth=self.auth
            )
            self.log.info('Requested job, return: %s' % req.status_code)
