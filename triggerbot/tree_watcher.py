# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import logging
import pprint
import re
import requests
import time

from threading import Timer
from collections import defaultdict
from thclient import TreeherderClient


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
    # Allow at least this many failures for a revision.
    # If we re-trigger for each orange and per-push orange
    # factor is approximately fixed, we shouldn't need to trigger
    # much more than that for any push that would be suitable to land.
    default_retry = 1
    per_push_failures = 4
    # We may trigger more than this as long as the total is below this
    # proportion of all builds for a push (~3% of jobs for now).
    failure_tolerance_factor = 33

    # See the comment below about pruning old revisions.
    revmap_threshold = 2000
    # If someone asks for more than 20 rebuilds on a push, only give them 20.
    # requested_limit = 20
    # Temporarily restrict to 5.
    requested_limit = 5

    def __init__(self, ldap_auth, is_triggerbot_user=lambda _: True):
        self.revmap = defaultdict(dict)
        self.revmap_threshold = TreeWatcher.revmap_threshold
        self.auth = ldap_auth
        self.lower_trigger_limit = TreeWatcher.default_retry * TreeWatcher.per_push_failures
        self.log = logging.getLogger('trigger-bot')
        self.is_triggerbot_user = is_triggerbot_user
        self.global_trigger_count = 0
        self.treeherder_client = TreeherderClient()
        self.hidden_builders = set()
        self.refresh_builder_counter = 0

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
                self.log.info('Finished pruning, oldest rev is now: %s' % rev)
                return

            del self.revmap[rev]
            prune_count -= 1

    def known_rev(self, branch, rev):
        return rev in self.revmap


    def _get_jobs(self, branch, rev, hidden):
        results = self.treeherder_client.get_resultsets(branch, revision=rev)
        jobs = []
        if results:
            result_set_id = results[0]['id']
            kwargs = {
                'count': 2000,
                'result_set_id': result_set_id,
            }
            if hidden:
                kwargs['visibility'] = 'excluded'
            jobs = self.treeherder_client.get_jobs(branch, **kwargs)
        return [job['ref_data_name'] for job in jobs
                if not re.match('[a-z0-9]{12}', job['ref_data_name'])]


    def get_hidden_jobs(self, branch, rev):
        return self._get_jobs(branch, rev, True)


    def get_visible_jobs(self, branch, rev):
        return self._get_jobs(branch, rev, False)


    def update_hidden_builders(self, branch, rev):
        hidden_builders = set(self.get_hidden_jobs(branch, rev))
        visible_builders = set(self.get_visible_jobs(branch, rev))
        self.hidden_builders -= visible_builders
        self.hidden_builders |= hidden_builders
        self.log.info('Updating hidden builders')
        self.log.info('There are %d hidden builders on try' %
                      len(self.hidden_builders))


    def failure_trigger(self, branch, rev, builder):

        if rev in self.revmap:

            if 'fail_retrigger' not in self.revmap[rev]:
                self.log.info('Found no request to retrigger %s on failure' %
                              rev)
                return

            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                self.log.info('We\'ve already seen "%s" at %s and don\'t'
                              ' need to trigger it' % (builder, rev))
                return

            if builder in self.hidden_builders:
                self.log.info('Would have triggered "%s" at %s due to failures,'
                              ' but that builder is hidden.' % (builder, rev))
                return

            seen_builders.add(builder)

            count = self.revmap[rev]['fail_retrigger']
            seen = self.revmap[rev]['rev_trigger_count']

            triggered = self.attempt_triggers(branch, rev, builder, count, seen)
            if triggered:
                self.revmap[rev]['rev_trigger_count'] += triggered
                self.log.info('Triggered %d of "%s" at %s' % (triggered, builder, rev))


    def requested_trigger(self, branch, rev, builder):
        if rev in self.revmap and 'requested_trigger' in self.revmap[rev]:

            self.log.info('Found a request to trigger %s and may retrigger' % rev)
            seen_builders = self.revmap[rev]['seen_builders']

            if builder in seen_builders:
                self.log.info('We already triggered "%s" at %s don\'t need'
                            ' to do it again' % (builder, rev))
                return

            seen_builders.add(builder)
            count, talos_count = self.revmap[rev]['requested_trigger']
            if talos_count and 'talos' in builder:
                count = talos_count

            self.log.info('May trigger %d requested jobs for "%s" at %s' %
                        (count, builder, rev))
            self.attempt_triggers(branch, rev, builder, count)


    def add_rev(self, branch, rev, comments, user):

        req_count, req_talos_count, should_retry = self.triggers_from_msg(comments)

        # Only trigger based on a request or a failure, not both.
        if req_count or req_talos_count:
            self.log.info('Added %d triggers for %s' % (req_count, rev))
            self.revmap[rev]['requested_trigger'] = (req_count, req_talos_count)

        if should_retry and not req_count:
            # self.log.info('Adding default failure retries for %s' % rev)
            self.revmap[rev]['fail_retrigger'] = TreeWatcher.default_retry

        self.revmap[rev]['rev_trigger_count'] = 0

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


    def triggers_from_msg(self, msg):

        try_message = None
        all_try_args = None

        for line in msg.splitlines():
            if 'try: ' in line:
                # Autoland adds quotes to try strings that will confuse our
                # args later on.
                if line.startswith('"') and line.endswith('"'):
                    line = line[1:-1]
                # Allow spaces inside of [filter expressions]
                try_message = line.strip().split('try: ', 1)
                all_try_args = re.findall(r'(?:\[.*?\]|\S)+', try_message[1])
                break

        if not try_message:
            return 0

        parser = argparse.ArgumentParser()
        parser.add_argument('--rebuild', type=int, default=0)
        parser.add_argument('--rebuild-talos', type=int, dest='rebuild_talos',
                            default=0)
        parser.add_argument('--no-retry', action='store_false', dest='retry',
                            default=True)
        (args, _) = parser.parse_known_args(all_try_args)

        limit = TreeWatcher.requested_limit
        rebuilds = args.rebuild if args.rebuild < limit else limit
        rebuild_talos = args.rebuild_talos if args.rebuild_talos < limit else limit
        return rebuilds, rebuild_talos, args.retry


    def handle_message(self, key, branch, rev, builder, status, comments, user):
        if not self.known_rev(branch, rev) and comments:
            # First time we've seen this revision? Add it to known
            # revs and mark required triggers,
            self.add_rev(branch, rev, comments, user)

        if key.endswith('started'):
            # If the job is starting and a user requested unconditional
            # retriggers, process them right away.
            self.requested_trigger(branch, rev, builder)

        if status in (1, 2):
            # A failing job is a candidate to retrigger.
            self.failure_trigger(branch, rev, builder)

        if self.refresh_builder_counter == 0:
            self.update_hidden_builders(branch, rev)
            self.refresh_builder_counter = 300
        else:
            self.refresh_builder_counter -= 1


    def attempt_triggers(self, branch, rev, builder, count, seen=0, attempt=0):
        if not re.match('[a-z0-9]{12}', rev):
            self.log.error('%s doesn\'t look like a valid revision, can\'t trigger it' %
                           rev)
            return

        build_data = self._get_ids_for_rev(branch, rev, builder)

        if build_data is None:
            return

        found_buildid, found_requestid, builder_total, rev_total = build_data

        if builder_total > count:
            self.log.warning('Would have triggered %d of "%s" at %s, but we\'ve already'
                             ' found more requests than that for this builder/rev.' %
                             (count, builder, rev))
            return

        self.log.info("Found %s jobs total for %s" % (rev_total, rev))
        if (seen * self.failure_tolerance_factor > rev_total and
            seen > self.lower_trigger_limit):
            self.log.warning('Would have triggered "%s" at %s but there are already '
                             'too many failures.' % (builder, rev))
            return

        self.global_trigger_count += count
        self.log.warning('Up to %d total triggers have been performed by this service.' %
                         self.global_trigger_count)

        if not self.is_triggerbot_user(self.revmap[rev]['user']):
            self.log.warning('Would have triggered "%s" at %s %d times.' %
                             (builder, rev, count))
            self.log.warning('But %s is not a triggerbot user.' % self.revmap[rev]['user'])
            # Pretend we did these triggers, just for accounting purposes.
            return count

        self.log.info('attempt_triggers, attempt %d' % attempt)

        root_url = 'https://secure.pub.build.mozilla.org/buildapi/self-serve'
        payload = {
            'count': count,
        }

        if found_buildid:
            build_url = '%s/%s/build' % (root_url, branch)
            payload['build_id'] = found_buildid
        elif found_requestid:
            build_url = '%s/%s/request' % (root_url, branch)
            payload['request_id'] = found_requestid
        else:
            # For a short time after a job starts it seems there might not be
            # any info associated with this job/builder in.
            self.log.warning('Could not trigger "%s" at %s because there were '
                             'no builds found with that buildername to rebuild.' %
                             (builder, rev))

            if attempt > 4:
                self.log.warning('Already tried to find something to rebuild '
                                 'for "%s" at %s, giving up' % (builder, rev))
                return

            self.log.warning('Will re-attempt')
            tm = Timer(90, self.attempt_triggers,
                       args=[branch, rev, builder, count, seen, attempt + 1])
            tm.start()
            # Assume some subsequent attempt will be succesful for accounting
            # purposes.
            return count

        self._rebuild(build_url, payload)
        return count


    def _get_ids_for_rev(self, branch, rev, builder):
        # Get the request or build id associated with the given branch/rev/builder,
        # if any.
        root_url = 'https://secure.pub.build.mozilla.org/buildapi/self-serve'

        # First find the build_id for the job to rebuild
        build_info_url = '%s/%s/rev/%s?format=json' % (root_url, branch, rev)
        info_req = requests.get(build_info_url,
                                headers={'Accept': 'application/json'},
                                auth=self.auth)
        found_buildid = None
        found_requestid = None
        builder_total, rev_total = 0, 0

        try:
            results = info_req.json()
        except ValueError:
            self.log.error('Received an unexpected ValueError when retrieving '
                           'information about %s from buildapi.' % rev)
            self.log.error('Request status: %d' % info_req.status_code)
            return None

        for res in results:
            rev_total += 1
            if res['buildername'] == builder:
                builder_total += 1
                if 'build_id' in res and not found_buildid:
                    found_buildid = res['build_id']
                if 'request_id' in res and not found_requestid:
                    found_requestid = res['request_id']

        return found_buildid, found_requestid, builder_total, rev_total

    def _rebuild(self, build_url, payload):
        # Actually do the triggering for a url and payload and keep track of the result.
        self.log.info('Triggering url: %s' % build_url)
        self.log.debug('Triggering payload:\n\t%s' % payload)
        req = requests.post(
            build_url,
            headers={'Accept': 'application/json'},
            data=payload,
            auth=self.auth
        )
        self.log.info('Requested job, return: %s' % req.status_code)

