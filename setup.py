"""
Korail2 -- Korail (www.letskorail.com) wrapper for Python.
==========================================================

If you're reading this code, you should know what Korail is.

Just play. Have fun. Enjoy the Korail2!
```````````````````````````````````````

::

    >>> from korail2 import Korail

Links
`````

* `GitHub repository <http://github.com/carpedm20/korail2>`_
* `development version
  <http://github.com/carpedm20/korail2/zipball/master>`_
"""
import codecs

from setuptools import setup

version = '0.4.0'

with codecs.open('README.rst', 'r', encoding='utf8') as f:
    long_desc = f.read()

setup(
    name='korail2',
    packages=['korail2'],
    version=version,
    description='Korail(www.letskorail.com) wrapper for Python',
    long_description=long_desc,
    license='BSD License',
    author='Taehoon Kim',
    author_email='carpedm20@gmail.com',
    url='http://github.com/carpedm20/korail2',
    keywords=['Korail'],
    python_requires='>=3.8',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: Implementation :: CPython',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
    ],
    install_requires=[
        'requests',
        'pycryptodome',
    ],
    extras_require={
        # korail.py 헌팅 스크립트에서 .env 파일을 자동 로드하고 싶을 때
        'hunt': ['python-dotenv'],
        # bot.py 텔레그램 봇
        'bot': ['python-telegram-bot>=21', 'python-dotenv'],
        # 테스트
        'test': ['python-dotenv'],
    },
)
