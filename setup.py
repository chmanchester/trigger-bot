# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from setuptools import setup, find_packages

setup(
      name='trigger-bot',
      version='0.1',
      packages=find_packages(),
      entry_points={
          'console_scripts': [
              'run-trigger-bot = triggerbot.triggerbot_pulse:run',
           ],
      },
      install_requires=[
           'buildapi-client>=0.1'
           'mozillapulse',
           'mozci>=0.25.5',
           'requests',
           'treeherder-client>=2.0.1'
      ],
      description='A pulse service for triggering builds with mozci.',
      classifiers=['Intended Audience :: Developers',
                   'License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)',
                   'Natural Language :: English',
                   'Operating System :: OS Independent',
                   'Programming Language :: Python',
                   'Topic :: Software Development :: Libraries :: Python Modules',
                   ],
      keywords='mozilla',
      author='Chris Manchester',
      author_email='cmanchester@mozilla.com',
      license='MPL 2.0',
      include_package_data=True,
      zip_safe=False
)
