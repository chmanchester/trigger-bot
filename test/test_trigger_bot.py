# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import sys
import unittest

from mock import Mock
from collections import defaultdict


from triggerbot.tree_watcher import TreeWatcher


class with_sequence(object):
    # Seed a given test method with the specified sequence
    # of messages.
    def __init__(self, seq):
        self._seq = seq

    def __call__(self, f):
        inst = self
        def wrapped(self, *args, **kwargs):
            for key, branch, rev, builder, status, comments in inst._seq:
                self.tw.handle_message(key, branch, rev, builder, status,
                                       comments, "")
            f(self, *args, **kwargs)

        return wrapped


# Sequences: (<key>, <branch>, <rev>, <builder>, <status>, <comments>)

failure_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b1', 1, ''),
]

limit_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b1', 1, ''),
    ('started', 'try', 1, 'b2', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b2', 1, ''),
    ('started', 'try', 1, 'b3', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b3', 1, ''),
    ('started', 'try', 1, 'b4', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b4', 1, ''),
    ('started', 'try', 1, 'b5', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b5', 1, ''),
]

no_retry_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none --no-retry'),
    ('finished', 'try', 1, 'b1', 1, ''),
    ('started', 'try', 1, 'b2', None, 'try: -b o -p linux -u xpcshell -t none --no-retry'),
    ('finished', 'try', 1, 'b2', 1, ''),
    ('started', 'try', 1, 'b3', None, 'try: -b o -p linux -u xpcshell -t none --no-retry'),
    ('finished', 'try', 1, 'b3', 1, ''),
    ('started', 'try', 1, 'b4', None, 'try: -b o -p linux -u xpcshell -t none --no-retry'),
    ('finished', 'try', 1, 'b4', 1, ''),
    ('started', 'try', 1, 'b5', None, 'try: -b o -p linux -u xpcshell -t none --no-retry'),
    ('finished', 'try', 1, 'b5', 1, ''),
]

no_trigger_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b1', 0, ''),
    ('started', 'try', 1, 'b2', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b2', 0, ''),
    ('started', 'try', 1, 'b3', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b3', 3, ''),
    ('started', 'try', 1, 'b4', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b4', 4, ''),
    ('started', 'try', 1, 'b5', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b5', 5, ''),
    ('started', 'try', 1, 'b6', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('finished', 'try', 1, 'b6', 0, ''),
]

request_start_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none --rebuild 10'),
]

request_fail_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none --rebuild 10'),
    ('finished', 'try', 1, 'b1', 1, ''),
]

all_test_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u all -t none --rebuild 10'),
]

all_platform_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p all -u xpcshell -t none --rebuild 10'),
]

revmap_limit_sequence = [
    ('started', 'try', 1, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 2, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 3, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 4, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 5, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 6, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 7, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 8, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 9, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
    ('started', 'try', 10, 'b1', None, 'try: -b o -p linux -u xpcshell -t none'),
]

class TestTriggerBot(unittest.TestCase):

    def assert_triggers(self, branch, rev, builder, count):
        actual = self.triggers[(branch, rev, builder)]
        self.assertEqual(actual, count)

    def setUp(self):
        TreeWatcher.revmap_threshold = 9

        self.triggers = defaultdict(int)
        self.tw = TreeWatcher(('', ''))
        self.tw.trigger_limit = 6
        self.tw.auth = None

        def record_trigger(branch, rev, builder, count=0, attempt=0):
            self.triggers[(branch, rev, builder)] += count

        self.tw.attempt_triggers = Mock(side_effect=record_trigger)
        self.tw.get_excluded_jobs = Mock(return_value=[])

    @with_sequence(request_start_sequence)
    def test_requested_trigger_at_start(self):
        # Test that requested triggers are processed at the start of a job.
        self.assert_triggers('try', 1, 'b1', 10)

    @with_sequence(all_test_sequence)
    def test_all_test_trigger_limit(self):
        # Test that "-u all" limits --rebuild to 1 retriggers.
        self.assert_triggers('try', 1, 'b1', 1)

    @with_sequence(all_platform_sequence)
    def test_all_platform_trigger_limit(self):
        # Test that "-p all" limits --rebuild to 5 retriggers.
        self.assert_triggers('try', 1, 'b1', 5)

    @with_sequence(request_fail_sequence)
    def test_requested_trigger_with_fail(self):
        # Test that requested triggers make a failure
        # ineligible for further triggers.
        self.assert_triggers('try', 1, 'b1', 10)

    @with_sequence(failure_sequence)
    def test_failure_triggers(self):
        # Test that a failing job results in retriggers.
        self.assert_triggers('try', 1, 'b1', TreeWatcher.default_retry)

    @with_sequence(revmap_limit_sequence)
    def test_prune_revmap(self):
        # Test that old revisions are pruned when necessary.
        self.assertEqual(6, len(self.tw.revmap.keys()))
        self.assertNotIn(1, self.tw.revmap)
        self.assertNotIn(2, self.tw.revmap)
        self.assertNotIn(3, self.tw.revmap)
        self.assertNotIn(4, self.tw.revmap)
        self.assertIn(5, self.tw.revmap)

    @with_sequence(no_trigger_sequence)
    def test_no_triggers(self):
        # Test that a passing job results in no triggers.
        self.assertEqual(0, sum(self.triggers.values()))

    @with_sequence(no_retry_sequence)
    def test_no_retriggers(self):
        self.assertEqual(0, sum(self.triggers.values()))


if __name__ == '__main__':
    unittest.main(verbosity=3)
