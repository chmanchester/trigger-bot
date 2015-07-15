# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import requests
import pprint
import json


# A really simple script to get stats from buildapi on how many builds
# are triggered by the trigger-bot.

tb_user = 'mozci-bot@mozilla.com'
base_url = 'https://secure.pub.build.mozilla.org/buildapi/self-serve'

CONF_PATH = '../scratch/conf.json'


def read_ldap_auth():
    with open(CONF_PATH) as f:
        conf = json.load(f)
        return conf['ldap_user'], conf['ldap_pw']

auth = read_ldap_auth()

try_jobs = '/try?date=%(year)s-%(month)s-%(day)s&format=json'

def jobs_by_day(day, month, year):
    url = '%s%s' % (base_url, try_jobs % locals())
    info_req = requests.get(url, auth=auth)
    return info_req.json()

def triggerbot_jobs(jobs):
    tbot_reason = 'Self-serve: Rebuilt by mozci-bot@mozilla.com'
    return [j for j in jobs
            if 'requests' in j and j['requests'][0]['reason'] == tbot_reason or
            'reason' in j and j['reason'] == tbot_reason]

def failed_jobs(jobs):
    return [j for j in jobs
            if 'status' in j and j['status'] in (1, 2)]

def passed_jobs(jobs):
    return [j for j in jobs
            if 'status' in j and j['status'] == 0]

def jobs_by_month(month, year, days):
    # Get stats on the first "days" days in the given month.
    all_jobs = 0
    tbot_jobs = 0
    failed_tbot_jobs = 0
    passed_tbot_jobs = 0

    for i in range(days):
        day = str(i + 1)
        if len(day) == 1:
            day = '0' + day
        jobs = jobs_by_day(day, month, year)
        print '%s jobs on %s %s' % (len(jobs), month, day)
        all_jobs += len(jobs)
        tjobs = triggerbot_jobs(jobs)
        print '\t%s trigger-bot jobs on %s %s' % (len(tjobs), month, day)
        print '\t(%s %%)' % ((len(tjobs)/float(len(jobs))) * 100)
        tbot_jobs += len(tjobs)
        failed_tbot_jobs += len(failed_jobs(tjobs))
        passed_tbot_jobs += len(passed_jobs(tjobs))

    return all_jobs, tbot_jobs, failed_tbot_jobs, passed_tbot_jobs


days, month, year = 14, '07', '2015'
all, tbot, fails, passes = jobs_by_month(month, year, days)

print """
Summary for the first %d days of %s %s:
\t%d jobs on try
\t%d jobs initiated by trigger-bot on try (%s%% of all)
\t%d jobs initiated by trigger-bot failed (%s%% of trigger bot jobs)
\t%d jobs initiated by trigger-bot passed (%s%% of trigger bot jobs)

""" % (days, month, year,
       all, tbot, (tbot/float(all)) * 100,
       fails, (fails/float(tbot)) * 100,
       passes, (passes/float(tbot)) * 100)

